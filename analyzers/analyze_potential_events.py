from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from time import monotonic
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import Json
import pystache
from dotenv import load_dotenv
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

from analyzers.analyze_concert_programs import validate_model
from analyzers.event_inclusion import (
    ALL_CATEGORIES,
    CLASSICAL_CATEGORY_ORDER,
    INCLUSION_DECISIONS,
    NONCLASSICAL_CATEGORY_ORDER,
    NOT_EVENT_CATEGORY_ORDER,
    UNCERTAIN_CATEGORY,
    load_inclusion_guidance,
    validate_decision_category,
)
from automation.codex_auth import CodexAuthRequiredError, raise_for_codex_auth
from crawlers.classical import CONCERT_INSERT_ADVISORY_LOCK
from observability import configure_logging


load_dotenv()
logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_MAX_EVENTS_PER_TURN = 50
DEFAULT_MAX_INPUT_CHARS = 120_000
DEFAULT_TURN_TIMEOUT_SECONDS = 1800
INTERRUPT_TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 3
UNCERTAIN_RETRY_INTERVAL_DAYS = 7
TECHNICAL_RETRY_INTERVAL_HOURS = 1
MAX_REPAIR_TURNS = 2
ADVISORY_LOCK_NAME = "classical-bot-potential-event-classification"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "analyze_potential_events.mustache"

FINDING_CODES = (
    "malformed_date",
    "duplicate_ingestion",
    "non_event_ingestion",
    "wrong_event_url",
    "missing_fields",
    "location_conflict",
    "unrelated_category",
    "polluted_field",
    "other",
)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "event_ids": {"type": "array", "items": {"type": "integer"}},
                    "decision": {
                        "type": "string",
                        "enum": list(INCLUSION_DECISIONS),
                    },
                    "category": {"type": "string", "enum": ALL_CATEGORIES},
                    "rationale": {"type": "string"},
                    "evidence_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "event_ids",
                    "decision",
                    "category",
                    "rationale",
                    "evidence_urls",
                ],
            },
        },
        "source_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string", "enum": list(FINDING_CODES)},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "error"],
                    },
                    "event_ids": {"type": "array", "items": {"type": "integer"}},
                    "summary": {"type": "string"},
                    "evidence_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["code", "severity", "event_ids", "summary", "evidence_urls"],
            },
        },
    },
    "required": ["classifications", "source_findings"],
}


@dataclass(frozen=True)
class PotentialEvent:
    id: int
    title: str
    date: date
    url: str
    source: str | None
    source_url: str | None
    time_from: time | None
    time_to: time | None
    city_raw: str | None
    country_code_raw: str | None
    venue: str | None
    type: str | None
    description: str | None


@dataclass(frozen=True)
class CandidateGroup:
    title: str
    description: str | None
    type: str | None
    venue: str | None
    occurrences: tuple[PotentialEvent, ...]

    @property
    def event_ids(self) -> list[int]:
        return [event.id for event in self.occurrences]

    def prompt_value(self) -> dict[str, Any]:
        return {
            "event_ids": self.event_ids,
            "title": self.title,
            "description": self.description,
            "type": self.type,
            "venue": self.venue,
            "occurrences": [
                {
                    "id": event.id,
                    "date": event.date.isoformat(),
                    "time_from": event.time_from.isoformat(timespec="minutes") if event.time_from else None,
                    "time_to": event.time_to.isoformat(timespec="minutes") if event.time_to else None,
                    "url": event.url,
                    "city_raw": event.city_raw,
                    "country_code_raw": event.country_code_raw,
                }
                for event in self.occurrences
            ],
        }


def get_connection():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        keepalives=1,
        keepalives_idle=60,
        keepalives_interval=20,
        keepalives_count=3,
    )


def acquire_lock(conn) -> bool:
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (ADVISORY_LOCK_NAME,))
        return bool(cursor.fetchone()[0])


def release_lock(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (ADVISORY_LOCK_NAME,))


def recover_stale_runs(conn) -> int:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE potential_event_classification_run
            SET status = CASE
                    WHEN classified_count + uncertain_count > 0 THEN 'partial'
                    ELSE 'failed' END,
                last_error = COALESCE(last_error, 'Classifier process ended before the run finished'),
                finished_at = now()
            WHERE status = 'running'
            """
        )
        count = cursor.rowcount
    conn.commit()
    return count


def eligibility_sql(*, include_past: bool, force: bool, reanalyze: bool = False) -> str:
    date_clause = "TRUE" if include_past else "p.date >= CURRENT_DATE"
    if reanalyze:
        return date_clause
    if force:
        analysis_clause = "(a.potential_event_id IS NULL OR a.status IN ('uncertain', 'error', 'failed'))"
    else:
        analysis_clause = """
            (a.potential_event_id IS NULL OR (
                a.status IN ('uncertain', 'error')
                AND a.attempts < 3
                AND (a.next_attempt_at IS NULL OR a.next_attempt_at <= now())
            ))
        """
    return f"p.analyzed = false AND {date_clause} AND {analysis_clause}"


def choose_source(
    conn,
    *,
    source: str | None,
    source_url: str | None,
    include_past: bool,
    force: bool,
    reanalyze: bool = False,
) -> tuple[str | None, str | None] | None:
    eligibility = eligibility_sql(
        include_past=include_past,
        force=force,
        reanalyze=reanalyze,
    )
    with conn.cursor() as cursor:
        if source is not None:
            params: list[Any] = [source]
            url_clause = ""
            if source_url is not None:
                url_clause = " AND p.source_url IS NOT DISTINCT FROM %s"
                params.append(source_url)
            cursor.execute(
                f"""
                SELECT p.source, p.source_url
                FROM potential_event p
                LEFT JOIN potential_event_classification a ON a.potential_event_id = p.id
                WHERE {eligibility}
                  AND p.source IS NOT DISTINCT FROM %s{url_clause}
                ORDER BY p.source_url NULLS LAST
                LIMIT 1
                """,
                params,
            )
        else:
            cursor.execute(
                f"""
                SELECT p.source, p.source_url
                FROM potential_event p
                LEFT JOIN potential_event_classification a ON a.potential_event_id = p.id
                WHERE {eligibility}
                GROUP BY p.source, p.source_url
                ORDER BY MIN(p.date) FILTER (WHERE p.date >= CURRENT_DATE) NULLS LAST,
                         MAX(p.date) DESC,
                         p.source NULLS LAST,
                         p.source_url NULLS LAST
                LIMIT 1
                """
            )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else None


def select_source_events(
    conn,
    source: str | None,
    source_url: str | None,
    *,
    include_past: bool,
    force: bool,
    reanalyze: bool = False,
) -> list[PotentialEvent]:
    eligibility = eligibility_sql(
        include_past=include_past,
        force=force,
        reanalyze=reanalyze,
    )
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT p.id, p.title, p.date, p.url, p.source, p.source_url,
                   p.time_from, p.time_to, p.city_raw, p.country_code_raw,
                   p.venue, p.type, p.description
            FROM potential_event p
            LEFT JOIN potential_event_classification a ON a.potential_event_id = p.id
            WHERE {eligibility}
              AND p.source IS NOT DISTINCT FROM %s
              AND p.source_url IS NOT DISTINCT FROM %s
            ORDER BY CASE WHEN p.date >= CURRENT_DATE THEN 0 ELSE 1 END,
                     CASE WHEN p.date >= CURRENT_DATE THEN p.date END ASC,
                     CASE WHEN p.date < CURRENT_DATE THEN p.date END DESC,
                     p.id
            """,
            (source, source_url),
        )
        return [PotentialEvent(*row) for row in cursor.fetchall()]


def candidate_groups(events: Iterable[PotentialEvent]) -> list[CandidateGroup]:
    grouped: dict[tuple[str, str | None, str | None, str | None], list[PotentialEvent]] = {}
    for event in events:
        key = (event.title, event.description, event.type, event.venue)
        grouped.setdefault(key, []).append(event)
    return [
        CandidateGroup(key[0], key[1], key[2], key[3], tuple(values))
        for key, values in grouped.items()
    ]


def split_large_group(group: CandidateGroup, maximum_events: int) -> list[CandidateGroup]:
    events = group.occurrences
    return [
        CandidateGroup(group.title, group.description, group.type, group.venue, events[index:index + maximum_events])
        for index in range(0, len(events), maximum_events)
    ]


def pack_pages(
    groups: list[CandidateGroup],
    *,
    maximum_events: int,
    maximum_chars: int,
) -> list[list[CandidateGroup]]:
    if maximum_events < 1 or maximum_chars < 1:
        raise ValueError("page limits must be positive")
    expanded = [part for group in groups for part in split_large_group(group, maximum_events)]
    pages: list[list[CandidateGroup]] = []
    page: list[CandidateGroup] = []
    event_count = 0
    char_count = 0
    for group in expanded:
        serialized = json.dumps(group.prompt_value(), ensure_ascii=False, default=str)
        group_events = len(group.occurrences)
        size = len(serialized)
        if page and (event_count + group_events > maximum_events or char_count + size > maximum_chars):
            pages.append(page)
            page = []
            event_count = 0
            char_count = 0
        page.append(group)
        event_count += group_events
        char_count += size
    if page:
        pages.append(page)
    return pages


def render_prompt(
    *,
    source: str | None,
    source_url: str | None,
    page: list[CandidateGroup],
    page_number: int,
    page_count: int,
) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    values = [group.prompt_value() for group in page]
    return pystache.render(
        template,
        {
            "source": source or "(missing)",
            "source_url": source_url or "(missing)",
            "page_number": page_number,
            "page_count": page_count,
            "event_count": sum(len(group.occurrences) for group in page),
            "candidate_json": json.dumps(values, ensure_ascii=False, indent=2),
            "event_inclusion_guidance": load_inclusion_guidance(),
            "classical_categories": list(CLASSICAL_CATEGORY_ORDER),
            "nonclassical_categories": list(NONCLASSICAL_CATEGORY_ORDER),
            "not_event_categories": list(NOT_EVENT_CATEGORY_ORDER),
            "uncertain_category": UNCERTAIN_CATEGORY,
        },
    )


def validate_result(page: list[CandidateGroup], result: dict[str, Any]) -> dict[str, Any]:
    expected = {event_id for group in page for event_id in group.event_ids}
    seen: set[int] = set()
    for classification in result["classifications"]:
        ids = classification["event_ids"]
        if not ids:
            raise ValueError("classification event_ids must not be empty")
        decision = classification["decision"]
        category = classification["category"]
        validate_decision_category(decision, category)
        if not classification["rationale"].strip():
            raise ValueError("classification rationale must not be empty")
        if not classification["evidence_urls"] or not all(
            value.strip() for value in classification["evidence_urls"]
        ):
            raise ValueError("classification must include at least one evidence URL")
        for event_id in ids:
            if event_id not in expected:
                raise ValueError(f"unknown potential event ID {event_id}")
            if event_id in seen:
                raise ValueError(f"duplicate potential event ID {event_id}")
            seen.add(event_id)
    missing = expected - seen
    if missing:
        raise ValueError(f"missing classifications for potential event IDs {sorted(missing)}")
    decision_by_event = {
        event_id: classification["decision"]
        for classification in result["classifications"]
        for event_id in classification["event_ids"]
    }
    for finding in result["source_findings"]:
        unknown = set(finding["event_ids"]) - expected
        if unknown:
            raise ValueError(f"source finding references unknown IDs {sorted(unknown)}")
        if not finding["summary"].strip():
            raise ValueError("source finding summary must not be empty")
        decisions = {decision_by_event[event_id] for event_id in finding["event_ids"]}
        if finding["code"] == "non_event_ingestion" and decisions != {"not_event"}:
            raise ValueError(
                "non_event_ingestion findings require not_event decisions for every affected ID"
            )
        if (
            finding["severity"] == "error"
            and finding["code"] in {"malformed_date", "wrong_event_url", "polluted_field"}
            and not decisions.issubset({"not_event", "uncertain"})
        ):
            raise ValueError(
                f"error-severity {finding['code']} findings require not_event or uncertain decisions"
            )
    return result


def repair_prompt(error: Exception, page: list[CandidateGroup]) -> str:
    expected = [event_id for group in page for event_id in group.event_ids]
    return (
        "Your previous response failed deterministic validation. Return a complete corrected "
        "response for the same page, not a patch. Do not mention the correction outside the "
        "structured response.\n\n"
        f"Validation error: {error}\n"
        f"Every expected event ID must appear exactly once: {expected}"
    )


async def run_turn(thread, prompt: str, model: str, timeout_seconds: int) -> dict[str, Any]:
    turn = await thread.turn(
        prompt,
        approval_mode=ApprovalMode.deny_all,
        cwd=str(Path.cwd()),
        model=model,
        output_schema=OUTPUT_SCHEMA,
        sandbox=Sandbox.workspace_write,
    )
    try:
        response = await asyncio.wait_for(turn.run(), timeout=timeout_seconds)
    except TimeoutError:
        try:
            await asyncio.wait_for(turn.interrupt(), timeout=INTERRUPT_TIMEOUT_SECONDS)
        except Exception as interrupt_error:
            raise RuntimeError("Timed-out potential-event turn could not be interrupted") from interrupt_error
        raise TimeoutError(f"Potential-event classification turn exceeded {timeout_seconds} seconds")
    if response.error:
        raise_for_codex_auth(str(response.error))
        raise RuntimeError(str(response.error))
    if not response.final_response:
        raise RuntimeError("Codex returned no final response")
    return json.loads(response.final_response)


async def run_validated_turn(
    thread,
    prompt: str,
    page: list[CandidateGroup],
    model: str,
    timeout_seconds: int,
    *,
    source: str | None,
    page_number: int,
) -> tuple[dict[str, Any], int]:
    repairs = 0
    current_prompt = prompt
    while True:
        result = await run_turn(thread, current_prompt, model, timeout_seconds)
        try:
            return validate_result(page, result), repairs
        except ValueError as error:
            if repairs >= MAX_REPAIR_TURNS:
                logger.error(
                    "Potential-event response repair exhausted",
                    extra={
                        "event": "potential_event_classification_repair_exhausted",
                        "source": source,
                        "page_number": page_number,
                        "repair_count": repairs,
                        "error_message": str(error),
                    },
                )
                raise
            repairs += 1
            logger.warning(
                "Repairing invalid potential-event response",
                extra={
                    "event": "potential_event_classification_repair_started",
                    "source": source,
                    "page_number": page_number,
                    "repair_number": repairs,
                    "error_message": str(error),
                },
            )
            current_prompt = repair_prompt(error, page)


def create_run(
    conn,
    *,
    source: str | None,
    source_url: str | None,
    scope: str,
    model: str,
    snapshot_count: int,
) -> int:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO potential_event_classification_run
                (source, source_url, scope, model, snapshot_count)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (source, source_url, scope, model, snapshot_count),
        )
        run_id = cursor.fetchone()[0]
    conn.commit()
    return run_id


def set_run_thread(conn, run_id: int, thread_id: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "UPDATE potential_event_classification_run SET thread_id = %s WHERE id = %s",
            (thread_id, run_id),
        )
    conn.commit()


def matching_concert(cursor, event_id: int) -> tuple[int, str] | None:
    cursor.execute(
        """
        SELECT c.id, c.inclusion_status
        FROM potential_event p
        JOIN classical_concert c
          ON c.title = p.title AND c.date = p.date AND c.url = p.url
        WHERE p.id = %s
        ORDER BY c.id
        LIMIT 1
        """,
        (event_id,),
    )
    return cursor.fetchone()


def promote_classical_event(cursor, event_id: int) -> tuple[str, int | None]:
    existing = matching_concert(cursor, event_id)
    if existing and existing[1] != "included":
        cursor.execute(
            """
            UPDATE potential_event
            SET analyzed = true, is_classical_concert = true,
                added = false, updated_at = now()
            WHERE id = %s
            """,
            (event_id,),
        )
        return "blocked", existing[0]
    if existing:
        cursor.execute(
            """
            UPDATE potential_event
            SET analyzed = true, is_classical_concert = true,
                added = true, updated_at = now()
            WHERE id = %s
            """,
            (event_id,),
        )
        return "promoted", existing[0]
    cursor.execute(
        """
        INSERT INTO classical_concert
            (title, date, url, source, source_url, time_from, time_to,
             city_raw, country_code_raw, city_id, country_code_resolved,
             venue, type, description)
        SELECT p.title, p.date, p.url, p.source, p.source_url, p.time_from, p.time_to,
               p.city_raw, p.country_code_raw, p.city_id, p.country_code_resolved,
               p.venue, p.type, p.description
        FROM potential_event p
        WHERE p.id = %s
          AND NOT EXISTS (
              SELECT 1 FROM classical_concert c
              WHERE c.title = p.title AND c.date = p.date AND c.url = p.url
          )
        RETURNING id
        """,
        (event_id,),
    )
    concert_id = cursor.fetchone()[0]
    cursor.execute(
        """
        UPDATE potential_event
        SET analyzed = true, is_classical_concert = true, added = true, updated_at = now()
        WHERE id = %s
        """,
        (event_id,),
    )
    return "promoted", concert_id


def promote_pending_classical_events(conn) -> tuple[int, int]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT p.id
            FROM potential_event p
            JOIN potential_event_classification a ON a.potential_event_id = p.id
            WHERE p.analyzed = true
              AND p.is_classical_concert = true
              AND p.added = false
              AND a.status = 'classified'
              AND a.is_classical = true
              AND NOT EXISTS (
                  SELECT 1
                  FROM classical_concert c
                  WHERE c.title = p.title AND c.date = p.date AND c.url = p.url
                    AND c.inclusion_status <> 'included'
              )
              AND EXISTS (
                  SELECT 1
                  FROM event_inclusion_assessment e
                  WHERE e.potential_event_id = p.id
                    AND e.origin = 'potential_classifier'
                    AND e.decision = 'classical'
                    AND e.id = (
                        SELECT MAX(e2.id)
                        FROM event_inclusion_assessment e2
                        WHERE e2.potential_event_id = p.id
                          AND e2.origin = 'potential_classifier'
                    )
              )
            ORDER BY p.id
            """
        )
        ids = [row[0] for row in cursor.fetchall()]
        if ids:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (CONCERT_INSERT_ADVISORY_LOCK,),
            )
        promoted = 0
        blocked = 0
        for event_id in ids:
            outcome, _ = promote_classical_event(cursor, event_id)
            promoted += outcome == "promoted"
            blocked += outcome == "blocked"
    conn.commit()
    return promoted, blocked


def insert_assessment(
    cursor,
    *,
    event_id: int,
    concert_id: int | None,
    run_id: int,
    classification: dict[str, Any],
    model: str,
) -> None:
    evidence_urls = classification["evidence_urls"]
    cursor.execute(
        """
        INSERT INTO event_inclusion_assessment
            (potential_event_id, classical_concert_id, classification_run_id,
             origin, decision, category, rationale, evidence_urls,
             source_url, model)
        VALUES (%s, %s, %s, 'potential_classifier', %s, %s, %s, %s, %s, %s)
        """,
        (
            event_id,
            concert_id,
            run_id,
            classification["decision"],
            classification["category"],
            classification["rationale"].strip(),
            Json(evidence_urls),
            evidence_urls[0] if evidence_urls else None,
            model,
        ),
    )


def quarantine_matching_concert(cursor, event_id: int) -> int | None:
    existing = matching_concert(cursor, event_id)
    if not existing:
        return None
    concert_id = existing[0]
    cursor.execute(
        """
        UPDATE classical_concert
        SET inclusion_status = 'quarantined',
            program_analysis_eligible = false,
            updated_at = now()
        WHERE id = %s AND inclusion_status = 'included'
        """,
        (concert_id,),
    )
    cursor.execute(
        "DELETE FROM classical_concert_work WHERE classical_concert_id = %s",
        (concert_id,),
    )
    cursor.execute(
        "DELETE FROM classical_concert_composer WHERE classical_concert_id = %s",
        (concert_id,),
    )
    return concert_id


def persist_page_result(
    conn,
    *,
    run_id: int,
    result: dict[str, Any],
    model: str,
    promote: bool = False,
    repairs: int = 0,
) -> tuple[int, int]:
    classified = 0
    uncertain = 0
    promoted = 0
    blocked = 0
    shadow_classical = 0
    try:
        with conn.cursor() as cursor:
            if promote and any(
                classification["decision"] == "classical"
                for classification in result["classifications"]
            ):
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (CONCERT_INSERT_ADVISORY_LOCK,),
                )
            for classification in result["classifications"]:
                decision = classification["decision"]
                for event_id in classification["event_ids"]:
                    if decision == "classical":
                        if promote:
                            promotion, concert_id = promote_classical_event(cursor, event_id)
                            promoted += promotion == "promoted"
                            blocked += promotion == "blocked"
                        else:
                            existing = matching_concert(cursor, event_id)
                            cursor.execute(
                                """
                                UPDATE potential_event
                                SET analyzed = true, is_classical_concert = true,
                                    added = %s, updated_at = now()
                                WHERE id = %s
                                """,
                                (
                                    bool(existing and existing[1] == "included"),
                                    event_id,
                                ),
                            )
                            concert_id = existing[0] if existing else None
                            shadow_classical += 1
                        is_classical = True
                        status = "classified"
                        next_attempt = None
                        completed = True
                        classified += 1
                    elif decision in {"nonclassical", "not_event"}:
                        concert_id = quarantine_matching_concert(cursor, event_id)
                        cursor.execute(
                            """
                            UPDATE potential_event
                            SET analyzed = true, is_classical_concert = false,
                                added = false, updated_at = now()
                            WHERE id = %s
                            """,
                            (event_id,),
                        )
                        is_classical = False
                        status = "classified"
                        next_attempt = None
                        completed = True
                        classified += 1
                    else:
                        existing = matching_concert(cursor, event_id)
                        concert_id = existing[0] if existing else None
                        cursor.execute(
                            """
                            UPDATE potential_event
                            SET analyzed = false, updated_at = now()
                            WHERE id = %s
                            """,
                            (event_id,),
                        )
                        is_classical = None
                        status = "uncertain"
                        next_attempt = f"{UNCERTAIN_RETRY_INTERVAL_DAYS} days"
                        completed = False
                        uncertain += 1
                    insert_assessment(
                        cursor,
                        event_id=event_id,
                        concert_id=concert_id,
                        run_id=run_id,
                        classification=classification,
                        model=model,
                    )
                    cursor.execute(
                        """
                        INSERT INTO potential_event_classification
                            (potential_event_id, latest_run_id, status, is_classical,
                             category, rationale, evidence_urls, attempts, model,
                             last_attempted_at, next_attempt_at, completed_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, now(),
                                CASE WHEN %s IS NULL THEN NULL ELSE now() + %s::interval END,
                                CASE WHEN %s THEN now() ELSE NULL END, now())
                        ON CONFLICT (potential_event_id) DO UPDATE SET
                            latest_run_id = EXCLUDED.latest_run_id,
                            status = EXCLUDED.status,
                            is_classical = EXCLUDED.is_classical,
                            category = EXCLUDED.category,
                            rationale = EXCLUDED.rationale,
                            evidence_urls = EXCLUDED.evidence_urls,
                            attempts = potential_event_classification.attempts + 1,
                            model = EXCLUDED.model,
                            last_error = NULL,
                            last_attempted_at = now(),
                            next_attempt_at = CASE
                                WHEN EXCLUDED.status = 'uncertain'
                                 AND potential_event_classification.attempts + 1 < %s
                                THEN now() + %s::interval ELSE NULL END,
                            completed_at = CASE
                                WHEN EXCLUDED.status = 'classified'
                                  OR potential_event_classification.attempts + 1 >= %s
                                THEN now() ELSE NULL END,
                            updated_at = now()
                        """,
                        (
                            event_id,
                            run_id,
                            status,
                            is_classical,
                            classification["category"],
                            classification["rationale"].strip(),
                            Json(classification["evidence_urls"]),
                            model,
                            next_attempt,
                            next_attempt,
                            completed,
                            MAX_ATTEMPTS,
                            f"{UNCERTAIN_RETRY_INTERVAL_DAYS} days",
                            MAX_ATTEMPTS,
                        ),
                    )
            cursor.execute(
                """
                UPDATE potential_event_classification_run
                SET classified_count = classified_count + %s,
                    uncertain_count = uncertain_count + %s,
                    repaired_count = repaired_count + %s,
                    promoted_count = promoted_count + %s,
                    blocked_promotion_count = blocked_promotion_count + %s,
                    shadow_classical_count = shadow_classical_count + %s,
                    findings = findings || %s::jsonb
                WHERE id = %s
                """,
                (
                    classified,
                    uncertain,
                    repairs,
                    promoted,
                    blocked,
                    shadow_classical,
                    Json(result["source_findings"]),
                    run_id,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return classified, uncertain


def persist_page_error(
    conn,
    *,
    run_id: int,
    event_ids: list[int],
    model: str,
    error: Exception,
) -> None:
    try:
        with conn.cursor() as cursor:
            for event_id in event_ids:
                cursor.execute(
                    """
                    INSERT INTO potential_event_classification
                        (potential_event_id, latest_run_id, status, attempts, model,
                         last_error, last_attempted_at, next_attempt_at, updated_at)
                    VALUES (%s, %s, 'error', 1, %s, %s, now(),
                            now() + %s::interval, now())
                    ON CONFLICT (potential_event_id) DO UPDATE SET
                        latest_run_id = EXCLUDED.latest_run_id,
                        status = CASE WHEN potential_event_classification.attempts + 1 >= %s
                                      THEN 'failed' ELSE 'error' END,
                        is_classical = NULL,
                        attempts = potential_event_classification.attempts + 1,
                        model = EXCLUDED.model,
                        last_error = EXCLUDED.last_error,
                        last_attempted_at = now(),
                        next_attempt_at = CASE
                            WHEN potential_event_classification.attempts + 1 < %s
                            THEN now() + %s::interval ELSE NULL END,
                        completed_at = CASE
                            WHEN potential_event_classification.attempts + 1 >= %s
                            THEN now() ELSE NULL END,
                        updated_at = now()
                    """,
                    (
                        event_id,
                        run_id,
                        model,
                        str(error),
                        f"{TECHNICAL_RETRY_INTERVAL_HOURS} hours",
                        MAX_ATTEMPTS,
                        MAX_ATTEMPTS,
                        f"{TECHNICAL_RETRY_INTERVAL_HOURS} hours",
                        MAX_ATTEMPTS,
                    ),
                )
            cursor.execute(
                """
                UPDATE potential_event_classification_run
                SET error_count = error_count + %s, last_error = %s
                WHERE id = %s
                """,
                (len(event_ids), str(error), run_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def finish_run(conn, run_id: int, *, failed: bool, error: Exception | None = None) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE potential_event_classification_run
            SET status = CASE
                    WHEN %s AND classified_count + uncertain_count > 0 THEN 'partial'
                    WHEN %s THEN 'failed' ELSE 'completed' END,
                last_error = COALESCE(%s, last_error), finished_at = now()
            WHERE id = %s
            """,
            (failed, failed, str(error) if error else None, run_id),
        )
    conn.commit()


def write_result(
    path: Path | None,
    *,
    status: str,
    selected_count: int,
    source: str | None,
    error: Exception | None = None,
) -> None:
    if path is None:
        return
    payload: dict[str, Any] = {
        "status": status,
        "selected_count": selected_count,
        "source_count": 1 if selected_count else 0,
        "source": source,
    }
    if error:
        payload.update(error_type=type(error).__name__, error_message=str(error))
        if isinstance(error, CodexAuthRequiredError):
            payload["auth_reason_code"] = error.reason_code
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def completion_status(failures: int, selected_count: int) -> str:
    if failures == 0:
        return "completed"
    if failures < selected_count:
        return "partial"
    return "fatal"


def normalize_codex_auth_error(error: Exception) -> Exception:
    if isinstance(error, CodexAuthRequiredError):
        return error
    try:
        raise_for_codex_auth(error)
    except CodexAuthRequiredError as auth_error:
        return auth_error
    return error


async def analyze_source(
    events: list[PotentialEvent],
    *,
    source: str | None,
    source_url: str | None,
    model: str,
    commit: bool,
    conn,
    run_id: int | None,
    maximum_events: int,
    maximum_chars: int,
    timeout_seconds: int,
    heartbeat_path: Path | None,
    promote: bool = False,
) -> int:
    pages = pack_pages(
        candidate_groups(events),
        maximum_events=maximum_events,
        maximum_chars=maximum_chars,
    )
    heartbeat = lambda: heartbeat_path.touch() if heartbeat_path else None
    failures = 0
    async with AsyncCodex(
        CodexConfig(
            codex_bin=os.getenv("CODEX_BIN"),
            cwd=str(Path.cwd()),
            config_overrides=("sandbox_workspace_write.network_access=true",),
        )
    ) as codex:
        await validate_model(codex, model)
        thread = await codex.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd=str(Path.cwd()),
            ephemeral=False,
            model=model,
            sandbox=Sandbox.workspace_write,
        )
        if commit and run_id is not None:
            set_run_thread(conn, run_id, thread.id)
        for index, page in enumerate(pages, start=1):
            heartbeat()
            event_ids = [event_id for group in page for event_id in group.event_ids]
            try:
                result, repairs = await run_validated_turn(
                    thread,
                    render_prompt(
                        source=source,
                        source_url=source_url,
                        page=page,
                        page_number=index,
                        page_count=len(pages),
                    ),
                    page,
                    model,
                    timeout_seconds,
                    source=source,
                    page_number=index,
                )
                if commit and run_id is not None:
                    persist_page_result(
                        conn,
                        run_id=run_id,
                        result=result,
                        model=model,
                        promote=promote,
                        repairs=repairs,
                    )
                if repairs:
                    logger.info(
                        "Potential-event response repaired",
                        extra={
                            "event": "potential_event_classification_repaired",
                            "source": source,
                            "page_number": index,
                            "repair_count": repairs,
                        },
                    )
            except (asyncio.CancelledError, CodexAuthRequiredError):
                raise
            except Exception as error:
                raise_for_codex_auth(error)
                failures += len(event_ids)
                logger.exception(
                    "Potential-event classification page failed",
                    extra={
                        "event": "potential_event_classification_page_failed",
                        "source": source,
                        "page_number": index,
                        "event_ids": event_ids,
                    },
                )
                if commit and run_id is not None:
                    persist_page_error(
                        conn,
                        run_id=run_id,
                        event_ids=event_ids,
                        model=model,
                        error=error,
                    )
            finally:
                heartbeat()
    return failures


def run(
    *,
    source: str | None = None,
    source_url: str | None = None,
    include_past: bool = False,
    force: bool = False,
    reanalyze: bool = False,
    commit: bool = False,
    promote: bool = False,
    model: str = DEFAULT_MODEL,
    maximum_events: int = DEFAULT_MAX_EVENTS_PER_TURN,
    maximum_chars: int = DEFAULT_MAX_INPUT_CHARS,
    timeout_seconds: int = DEFAULT_TURN_TIMEOUT_SECONDS,
    heartbeat_path: Path | None = None,
    result_path: Path | None = None,
) -> int:
    if promote and not commit:
        raise ValueError("promotion requires committed classification")
    if reanalyze and source is None:
        raise ValueError("source reanalysis requires an explicit source")
    started_at = monotonic()
    conn = get_connection()
    locked = False
    run_id: int | None = None
    events: list[PotentialEvent] = []
    selected_source: str | None = None
    try:
        if commit:
            locked = acquire_lock(conn)
            if not locked:
                raise RuntimeError("Another committed potential-event classification is running")
            recover_stale_runs(conn)
            if promote:
                promoted, blocked = promote_pending_classical_events(conn)
                logger.info(
                    "Processed pending validated potential-event promotions",
                    extra={
                        "event": "potential_event_pending_promotion_completed",
                        "promoted_count": promoted,
                        "blocked_count": blocked,
                    },
                )
        selected = choose_source(
            conn,
            source=source,
            source_url=source_url,
            include_past=include_past,
            force=force,
            reanalyze=reanalyze,
        )
        if selected is None:
            write_result(result_path, status="empty", selected_count=0, source=source)
            return 0
        selected_source, selected_source_url = selected
        events = select_source_events(
            conn,
            selected_source,
            selected_source_url,
            include_past=include_past,
            force=force,
            reanalyze=reanalyze,
        )
        if commit:
            run_id = create_run(
                conn,
                source=selected_source,
                source_url=selected_source_url,
                scope="all" if include_past else "future",
                model=model,
                snapshot_count=len(events),
            )
        logger.info(
            "Starting source-level potential-event classification",
            extra={
                "event": "potential_event_classification_started",
                "source": selected_source,
                "source_url": selected_source_url,
                "selected_count": len(events),
                "commit": commit,
                "promote": promote,
                "reanalyze": reanalyze,
            },
        )
        failures = asyncio.run(
            analyze_source(
                events,
                source=selected_source,
                source_url=selected_source_url,
                model=model,
                commit=commit,
                conn=conn,
                run_id=run_id,
                maximum_events=maximum_events,
                maximum_chars=maximum_chars,
                timeout_seconds=timeout_seconds,
                heartbeat_path=heartbeat_path,
                promote=promote,
            )
        )
        if commit and run_id is not None:
            finish_run(conn, run_id, failed=bool(failures))
        write_result(
            result_path,
            status=completion_status(failures, len(events)),
            selected_count=len(events),
            source=selected_source,
        )
        logger.info(
            "Source-level potential-event classification completed",
            extra={
                "event": "potential_event_classification_completed",
                "source": selected_source,
                "selected_count": len(events),
                "failure_count": failures,
                "duration_seconds": round(monotonic() - started_at, 3),
            },
        )
        return failures
    except Exception as caught_error:
        error = normalize_codex_auth_error(caught_error)
        if run_id is not None:
            try:
                finish_run(conn, run_id, failed=True, error=error)
            except Exception:
                conn.rollback()
                logger.exception("Could not mark potential-event classification run failed")
        write_result(
            result_path,
            status="auth_required" if isinstance(error, CodexAuthRequiredError) else "fatal",
            selected_count=len(events),
            source=selected_source or source,
            error=error,
        )
        raise error
    finally:
        if locked:
            try:
                release_lock(conn)
            except Exception:
                logger.exception("Could not release potential-event classification lock")
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify one potential-event source with a persistent Codex thread."
    )
    parser.add_argument("--source")
    parser.add_argument("--source-url")
    parser.add_argument("--include-past", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="Reassess all selected source rows, including completed classifications.",
    )
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Promote validated classical decisions; requires --commit.",
    )
    parser.add_argument("--model", default=os.getenv("POTENTIAL_EVENT_CODEX_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-events-per-turn", type=int, default=DEFAULT_MAX_EVENTS_PER_TURN)
    parser.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TURN_TIMEOUT_SECONDS)
    parser.add_argument("--heartbeat-path", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--result-path", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.source_url and args.source is None:
        parser.error("--source-url requires --source")
    if args.reanalyze and args.source is None:
        parser.error("--reanalyze requires --source")
    if args.promote and not args.commit:
        parser.error("--promote requires --commit")
    return args


def main() -> None:
    configure_logging("classical-bot")
    args = parse_args()
    failures = run(
        source=args.source,
        source_url=args.source_url,
        include_past=args.include_past,
        force=args.force,
        reanalyze=args.reanalyze,
        commit=args.commit,
        promote=args.promote,
        model=args.model,
        maximum_events=args.max_events_per_turn,
        maximum_chars=args.max_input_chars,
        timeout_seconds=args.timeout_seconds,
        heartbeat_path=args.heartbeat_path,
        result_path=args.result_path,
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
