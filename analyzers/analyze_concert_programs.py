from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import Json
import pystache
from dotenv import load_dotenv
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

from agent_utils.concert_catalog import normalize
from automation.codex_auth import (
    CodexAuthRequiredError,
    raise_for_codex_auth,
)
from crawlers.cities import normalize_city_key
from observability import configure_logging


load_dotenv()
logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_LIMIT: int | None = None
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT_SECONDS = 1800
INTERRUPT_TIMEOUT_SECONDS = 15
POSTGRES_KEEPALIVES_IDLE_SECONDS = 60
POSTGRES_KEEPALIVES_INTERVAL_SECONDS = 20
POSTGRES_KEEPALIVES_COUNT = 3
MAX_AUTOMATIC_ATTEMPTS = 3
NO_PROGRAM_RETRY_INTERVAL_DAYS = 7
TECHNICAL_RETRY_INTERVAL_HOURS = 1
ADVISORY_LOCK_NAME = "classical-sk-concert-program-analysis"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "analyze_concert_program.mustache"
EVENT_UPDATE_FIELDS = (
    "event_status",
    "date",
    "time_from",
    "time_to",
    "venue",
)
EVENT_STATUSES = ("scheduled", "cancelled", "postponed", "rescheduled")
CITY_EXTERNAL_SOURCE = "geonames"


class CodexClientUnhealthyError(RuntimeError):
    """Raised when a timed-out Codex turn cannot be interrupted safely."""


PROGRAMME_RESULT_PROPERTIES: dict[str, Any] = {
    "status": {
        "type": "string",
        "enum": [
            "complete",
            "partial",
            "composer_only",
            "ambiguous",
            "no_program",
            "page_unavailable",
        ],
    },
    "notes": {"type": "string"},
    "composers": {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "existing_id": {"type": ["integer", "null"]},
                "name": {"type": "string"},
            },
            "required": ["existing_id", "name"],
        },
    },
    "program": {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "composer": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "existing_id": {"type": ["integer", "null"]},
                        "name": {"type": "string"},
                    },
                    "required": ["existing_id", "name"],
                },
                "work": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "existing_id": {"type": ["integer", "null"]},
                        "title": {"type": "string"},
                        "catalogue_number": {"type": ["string", "null"]},
                    },
                    "required": ["existing_id", "title", "catalogue_number"],
                },
                "programme_label": {"type": "string"},
                "evidence": {"type": "string"},
            },
            "required": ["composer", "work", "programme_label", "evidence"],
        },
    },
    "unresolved_program": {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "programme_label": {"type": "string"},
                "evidence": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["programme_label", "evidence", "reason"],
        },
    },
}

EVENT_UPDATES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "field": {"type": "string", "enum": list(EVENT_UPDATE_FIELDS)},
            "new_value": {"type": "string"},
            "source_url": {"type": "string"},
            "evidence": {"type": "string"},
        },
        "required": ["field", "new_value", "source_url", "evidence"],
    },
}

LOCATION_RESOLUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["not_needed", "existing_city", "new_city", "country_only", "ambiguous", "invalid", "insufficient_evidence"]},
        "existing_city_id": {"type": ["integer", "null"]},
        "english_name": {"type": ["string", "null"]},
        "local_name": {"type": ["string", "null"]},
        "country_code": {"type": ["string", "null"]},
        "external_id": {"type": ["string", "null"]},
        "raw_value_type": {"type": ["string", "null"], "enum": ["legitimate_name", "postal_or_address", "extraction_artifact", "ambiguous", "invalid", None]},
        "source_url": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["status", "existing_city_id", "english_name", "local_name", "country_code", "external_id", "raw_value_type", "source_url", "evidence"],
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "programme_groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "concert_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    **PROGRAMME_RESULT_PROPERTIES,
                },
                "required": [
                    "concert_ids",
                    "status",
                    "notes",
                    "composers",
                    "program",
                    "unresolved_program",
                ],
            },
        },
        "concert_results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "concert_id": {"type": "integer"},
                    "source_url": {"type": "string"},
                    "event_updates": EVENT_UPDATES_SCHEMA,
                    "location_resolution": LOCATION_RESOLUTION_SCHEMA,
                },
                "required": [
                    "concert_id",
                    "source_url",
                    "event_updates",
                    "location_resolution",
                ],
            },
        },
    },
    "required": ["programme_groups", "concert_results"],
}


@dataclass(frozen=True)
class Concert:
    id: int
    title: str
    date: date
    url: str
    description: str | None
    time_from: time | None = None
    time_to: time | None = None
    city_raw: str | None = None
    country_code_raw: str | None = None
    venue: str | None = None
    event_status: str = "scheduled"
    source: str | None = None
    city_id: int | None = None
    city_english_name: str | None = None
    city_local_name: str | None = None
    country_code_resolved: str | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class ConcertGroup:
    key: tuple[str, str, str]
    concerts: tuple[Concert, ...]


def get_connection():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        keepalives=1,
        keepalives_idle=POSTGRES_KEEPALIVES_IDLE_SECONDS,
        keepalives_interval=POSTGRES_KEEPALIVES_INTERVAL_SECONDS,
        keepalives_count=POSTGRES_KEEPALIVES_COUNT,
    )


def group_concerts(concerts: list[Concert]) -> list[ConcertGroup]:
    grouped: dict[tuple[str, str, str], list[Concert]] = {}
    for concert in concerts:
        normalized_title = normalize(concert.title)
        if not concert.source or not normalized_title or not concert.source_url:
            key = (f"__singleton__:{concert.id}", normalized_title, "")
        else:
            key = (concert.source, normalized_title, concert.source_url)
        grouped.setdefault(key, []).append(concert)
    return [ConcertGroup(key, tuple(items)) for key, items in grouped.items()]


def render_prompt(group: ConcertGroup) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    description_labels: dict[str, str] = {}
    descriptions = []
    occurrences = []
    for concert in group.concerts:
        description = concert.description or "(not available)"
        if description not in description_labels:
            label = f"D{len(description_labels) + 1}"
            description_labels[description] = label
            descriptions.append({"label": label, "text": description})
        occurrences.append(
            {
                "id": concert.id,
                "title": concert.title,
                "date": concert.date.isoformat(),
                "url": concert.url,
                "description_label": description_labels[description],
                "time_from": (
                    concert.time_from.isoformat(timespec="minutes")
                    if concert.time_from
                    else "(not available)"
                ),
                "time_to": (
                    concert.time_to.isoformat(timespec="minutes")
                    if concert.time_to
                    else "(not available)"
                ),
                "city_raw": concert.city_raw or "(not available)",
                "country_code_raw": concert.country_code_raw or "(not available)",
                "city_id": concert.city_id or "(unresolved)",
                "city_english_name": concert.city_english_name or "(unresolved)",
                "city_local_name": concert.city_local_name or "(unresolved)",
                "country_code_resolved": concert.country_code_resolved or "(unresolved)",
                "venue": concert.venue or "(not available)",
                "event_status": concert.event_status,
            }
        )
    return pystache.render(
        template,
        {
            "source": group.concerts[0].source or "(not available)",
            "normalized_title": group.key[1],
            "grouping_source_url": group.concerts[0].source_url or "(not available)",
            "concert_count": len(group.concerts),
            "descriptions": descriptions,
            "occurrences": occurrences,
        },
    )


def select_concerts(
    conn,
    concert_ids: list[int] | None,
    limit: int | None,
    force: bool,
    unresolved_locations: bool = False,
) -> list[Concert]:
    columns = """c.id, c.title, c.date, c.url, c.description,
                 c.time_from, c.time_to, c.city_raw, c.country_code_raw, c.venue,
                 c.event_status, c.source, c.city_id, city.english_name,
                 city.local_name, c.country_code_resolved, c.source_url"""
    with conn.cursor() as cursor:
        if concert_ids:
            cursor.execute(
                """
                SELECT {columns}
                FROM classical_concert c
                LEFT JOIN city ON city.id = c.city_id
                LEFT JOIN concert_program_analysis a ON a.classical_concert_id = c.id
                WHERE c.id = ANY(%s)
                  AND (%s OR a.status IS NULL OR a.status NOT IN ('complete', 'partial', 'composer_only', 'ambiguous', 'expired_no_program', 'failed'))
                ORDER BY c.id
                LIMIT %s
                """.format(columns=columns),
                (concert_ids, force, limit),
            )
        elif unresolved_locations:
            cursor.execute(
                f"""SELECT {columns}
                    FROM classical_concert c
                    LEFT JOIN city ON city.id = c.city_id
                    WHERE c.city_id IS NULL AND c.city_raw IS NOT NULL
                      AND c.date >= CURRENT_DATE
                    ORDER BY c.date, c.id LIMIT %s""",
                (limit,),
            )
        else:
            cursor.execute(
                """
                SELECT {columns}
                FROM classical_concert c
                LEFT JOIN city ON city.id = c.city_id
                LEFT JOIN concert_program_analysis a ON a.classical_concert_id = c.id
                WHERE c.program_analysis_eligible = true
                  AND c.date >= CURRENT_DATE
                  AND (
                    a.id IS NULL
                    OR (
                      a.status IN ('no_program', 'partial', 'ambiguous')
                      AND a.attempts < %s
                      AND a.last_attempted_at <= now() - make_interval(days => %s)
                    )
                    OR (
                      a.status = 'page_unavailable'
                      AND a.attempts < %s
                      AND a.last_attempted_at <= now() - make_interval(days => %s)
                    )
                    OR (
                      a.status = 'error'
                      AND a.attempts < %s
                      AND a.last_attempted_at <= now() - make_interval(hours => %s)
                    )
                  )
                ORDER BY c.date, c.id
                LIMIT %s
                """.format(columns=columns),
                (
                    MAX_AUTOMATIC_ATTEMPTS,
                    NO_PROGRAM_RETRY_INTERVAL_DAYS,
                    MAX_AUTOMATIC_ATTEMPTS,
                    NO_PROGRAM_RETRY_INTERVAL_DAYS,
                    MAX_AUTOMATIC_ATTEMPTS,
                    TECHNICAL_RETRY_INTERVAL_HOURS,
                    limit,
                ),
            )
        return [Concert(*row) for row in cursor.fetchall()]


def expire_old_no_program(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE concert_program_analysis a
            SET status = 'expired_no_program', completed_at = now()
            FROM classical_concert c
            WHERE a.classical_concert_id = c.id
              AND a.status = 'no_program'
              AND c.date < CURRENT_DATE
            """
        )
    conn.commit()


def resolve_concurrency(cli_value: int | None = None) -> int:
    raw_value: int | str = (
        cli_value
        if cli_value is not None
        else os.getenv("CONCERT_PROGRAM_CONCURRENCY", DEFAULT_CONCURRENCY)
    )
    try:
        concurrency = int(raw_value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"must be a positive integer, got {raw_value!r}") from error
    if concurrency < 1:
        raise ValueError(f"must be at least 1, got {concurrency}")
    return concurrency


async def validate_model(codex: AsyncCodex, model: str) -> None:
    try:
        available = {item.model for item in (await codex.models()).data}
    except Exception as sdk_error:
        raise_for_codex_auth(sdk_error)
        cache_path = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")) / "models_cache.json"
        try:
            catalogue = json.loads(cache_path.read_text(encoding="utf-8"))
            available = {
                item.get("slug") or item.get("model")
                for item in catalogue.get("models", catalogue.get("data", []))
            }
            available.discard(None)
        except Exception as cache_error:
            raise RuntimeError(
                f"Could not validate Codex model {model!r}: SDK catalogue failed ({sdk_error}) "
                f"and {cache_path} could not be read ({cache_error})"
            ) from sdk_error
    if model not in available:
        raise RuntimeError(f"Codex model {model!r} is unavailable. Available models: {', '.join(sorted(available))}")


async def run_agent(
    codex: AsyncCodex,
    group: ConcertGroup,
    model: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    thread = await codex.thread_start(
        approval_mode=ApprovalMode.deny_all,
        cwd=str(Path.cwd()),
        ephemeral=False,
        model=model,
        sandbox=Sandbox.full_access,
    )
    turn = await thread.turn(
        render_prompt(group),
        approval_mode=ApprovalMode.deny_all,
        cwd=str(Path.cwd()),
        model=model,
        output_schema=OUTPUT_SCHEMA,
        sandbox=Sandbox.full_access,
    )
    try:
        result = await asyncio.wait_for(turn.run(), timeout=timeout_seconds)
    except TimeoutError:
        try:
            await asyncio.wait_for(
                turn.interrupt(),
                timeout=INTERRUPT_TIMEOUT_SECONDS,
            )
        except Exception as interrupt_error:
            logger.exception(
                "Could not interrupt timed-out Codex turn",
                extra={
                    "event": "programme_analysis_interrupt_failed",
                    "concert_ids": [concert.id for concert in group.concerts],
                },
            )
            raise CodexClientUnhealthyError(
                "Timed-out Codex turn could not be interrupted safely"
            ) from interrupt_error
        raise TimeoutError(
            f"Concert group programme analysis exceeded {timeout_seconds} seconds"
        )
    if result.error:
        raise_for_codex_auth(str(result.error))
        raise RuntimeError(str(result.error))
    if not result.final_response:
        raise RuntimeError("Codex returned no final response")
    return json.loads(result.final_response)


def expand_group_result(
    group: ConcertGroup,
    result: dict[str, Any],
) -> list[tuple[Concert, dict[str, Any]]]:
    expected_ids = {concert.id for concert in group.concerts}
    programme_by_id: dict[int, dict[str, Any]] = {}
    for programme_group in result["programme_groups"]:
        concert_ids = programme_group["concert_ids"]
        if not concert_ids:
            raise ValueError("Programme groups must not be empty")
        for concert_id in concert_ids:
            if concert_id not in expected_ids:
                raise ValueError(f"Unknown concert ID {concert_id} in programme groups")
            if concert_id in programme_by_id:
                raise ValueError(f"Duplicate concert ID {concert_id} in programme groups")
            programme_by_id[concert_id] = programme_group

    occurrence_by_id: dict[int, dict[str, Any]] = {}
    for occurrence in result["concert_results"]:
        concert_id = occurrence["concert_id"]
        if concert_id not in expected_ids:
            raise ValueError(f"Unknown concert ID {concert_id} in concert results")
        if concert_id in occurrence_by_id:
            raise ValueError(f"Duplicate concert ID {concert_id} in concert results")
        occurrence_by_id[concert_id] = occurrence

    missing_programmes = expected_ids - programme_by_id.keys()
    missing_occurrences = expected_ids - occurrence_by_id.keys()
    if missing_programmes:
        raise ValueError(
            f"Missing programme results for concert IDs {sorted(missing_programmes)}"
        )
    if missing_occurrences:
        raise ValueError(
            f"Missing occurrence results for concert IDs {sorted(missing_occurrences)}"
        )

    expanded = []
    for concert in group.concerts:
        programme = programme_by_id[concert.id]
        occurrence = occurrence_by_id[concert.id]
        expanded.append(
            (
                concert,
                {
                    "status": programme["status"],
                    "source_url": occurrence["source_url"],
                    "notes": programme["notes"],
                    "composers": programme["composers"],
                    "program": programme["program"],
                    "unresolved_program": programme["unresolved_program"],
                    "event_updates": occurrence["event_updates"],
                    "location_resolution": occurrence["location_resolution"],
                },
            )
        )
    return expanded


def validate_result(conn, concert: Concert, result: dict[str, Any]) -> None:
    status = result["status"]
    composers = result["composers"]
    program = result["program"]
    unresolved_program = result["unresolved_program"]
    if status == "complete" and not program:
        raise ValueError("A complete result must contain at least one composer/work pair")
    if status == "complete" and not composers:
        raise ValueError("A complete result must contain its identified composers")
    if status == "complete" and unresolved_program:
        raise ValueError("A complete result must not contain unresolved programme entries")
    if status == "partial" and (not composers or not program or not unresolved_program):
        raise ValueError(
            "A partial result must contain composers, confident programme entries, and unresolved entries"
        )
    if status == "ambiguous" and (program or not unresolved_program):
        raise ValueError(
            "An ambiguous result must contain unresolved entries and no confident programme entries"
        )
    if status == "composer_only" and (not composers or program):
        raise ValueError("A composer_only result must contain composers and no programme entries")
    if status in {"no_program", "page_unavailable"} and (composers or program):
        raise ValueError(f"A {status} result must not contain catalogue entries")
    if status in {"composer_only", "no_program", "page_unavailable"} and unresolved_program:
        raise ValueError(f"A {status} result must not contain unresolved programme entries")
    if result["source_url"].strip() == "":
        raise ValueError("source_url must not be empty")
    for entry in unresolved_program:
        if not all(entry[field].strip() for field in ("programme_label", "evidence", "reason")):
            raise ValueError("Unresolved programme fields must not be empty")
    if status in {"ambiguous", "no_program", "page_unavailable"}:
        return
    with conn.cursor() as cursor:
        for composer in composers:
            _validate_composer(cursor, composer)
        listed_composers = {_composer_identity(composer) for composer in composers}
        for entry in program:
            composer = entry["composer"]
            work = entry["work"]
            if not composer["name"].strip() or not work["title"].strip():
                raise ValueError("Composer names and work titles must not be empty")
            if not entry["programme_label"].strip():
                raise ValueError("Programme labels must not be empty")
            _validate_composer(cursor, composer)
            if _composer_identity(composer) not in listed_composers:
                raise ValueError("Every programme composer must appear in the top-level composers list")
            if work["existing_id"] is not None:
                cursor.execute("SELECT composer_id FROM work WHERE id = %s", (work["existing_id"],))
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(f"Unknown work ID {work['existing_id']}")
                if composer["existing_id"] is None or row[0] != composer["existing_id"]:
                    raise ValueError("Existing work does not belong to the selected existing composer")


def validate_event_updates(
    conn,
    concert: Concert,
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    accepted = []
    seen_fields = set()
    current_values = {
        "event_status": concert.event_status,
        "date": concert.date,
        "time_from": concert.time_from,
        "time_to": concert.time_to,
        "venue": concert.venue,
    }
    for update in updates:
        try:
            field = update["field"]
            if field in seen_fields:
                raise ValueError(f"duplicate update for {field}")
            seen_fields.add(field)
            source_url = update["source_url"].strip()
            evidence = update["evidence"].strip()
            raw_value = update["new_value"].strip()
            if not source_url or not evidence or not raw_value:
                raise ValueError("new_value, source_url, and evidence must not be empty")

            if field == "event_status":
                if raw_value not in EVENT_STATUSES:
                    raise ValueError(f"unsupported event status {raw_value!r}")
                db_value = raw_value
            elif field == "date":
                db_value = date.fromisoformat(raw_value)
            elif field in {"time_from", "time_to"}:
                if not re.fullmatch(r"\d{2}:\d{2}", raw_value):
                    raise ValueError("times must use HH:MM")
                db_value = time.fromisoformat(raw_value)
            elif field == "venue":
                db_value = raw_value
            else:
                raise ValueError(f"unsupported event field {field!r}")

            comparison_value = current_values[field]
            if db_value == comparison_value:
                raise ValueError("proposed value is unchanged")
            if field == "date":
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT 1
                        FROM classical_concert
                        WHERE id <> %s AND title = %s AND url = %s AND date = %s
                        LIMIT 1
                        """,
                        (concert.id, concert.title, concert.url, db_value),
                    )
                    if cursor.fetchone() is not None:
                        raise ValueError("another concert already has the proposed title, URL, and date")

            accepted.append(
                {
                    "field": field,
                    "db_value": db_value,
                    "new_value": raw_value,
                    "source_url": source_url,
                    "evidence": evidence,
                    "old_value": _json_event_value(current_values[field]),
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            logger.warning(
                "Ignoring invalid event update",
                extra={
                    "event": "programme_event_update_invalid",
                    "concert_id": concert.id,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
    return accepted


def validate_location_resolution(
    conn,
    concert: Concert,
    proposal: dict[str, Any],
    *,
    page_available: bool = True,
) -> dict[str, Any] | None:
    try:
        status = proposal["status"]
        if status in {"not_needed", "ambiguous", "invalid", "insufficient_evidence"}:
            return None
        if not page_available:
            raise ValueError("an unavailable page cannot resolve a location")
        source_url = proposal["source_url"].strip()
        evidence = proposal["evidence"].strip()
        country = (proposal.get("country_code") or "").strip()
        if not source_url.startswith(("http://", "https://")) or not evidence:
            raise ValueError("location evidence and an HTTP source URL are required")
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("location country must be uppercase ISO alpha-2")
        if status == "country_only":
            if concert.city_id is not None:
                raise ValueError("country-only resolution cannot override a resolved city")
            return {"status": status, "country_code": country, "source_url": source_url, "evidence": evidence}
        raw_type = proposal.get("raw_value_type")
        if raw_type not in {"legitimate_name", "postal_or_address", "extraction_artifact"}:
            raise ValueError("resolved locations require a resolvable raw value type")
        if status == "existing_city":
            city_id = proposal.get("existing_city_id")
            with conn.cursor() as cursor:
                cursor.execute("SELECT english_name, local_name, country_code FROM city WHERE id = %s", (city_id,))
                row = cursor.fetchone()
            if row is None:
                raise ValueError("unknown existing city ID")
            if row[2] != country:
                raise ValueError("city and country conflict")
            return {"status": status, "city_id": city_id, "country_code": country, "raw_value_type": raw_type, "source_url": source_url, "evidence": evidence}
        if status != "new_city":
            raise ValueError("unsupported location status")
        required = ("english_name", "local_name", "external_id")
        values = {field: (proposal.get(field) or "").strip() for field in required}
        if not all(values.values()):
            raise ValueError("new cities require names and a GeoNames identity")
        if not re.fullmatch(r"[1-9][0-9]*", values["external_id"]):
            raise ValueError("new cities require a positive numeric GeoNames ID")
        parsed_source_url = urlparse(source_url)
        if (
            parsed_source_url.scheme != "https"
            or (parsed_source_url.hostname or "").lower()
            not in {"geonames.org", "www.geonames.org"}
            or not re.match(
                rf"^/{re.escape(values['external_id'])}(?:/|$)",
                parsed_source_url.path,
            )
        ):
            raise ValueError("new city source URL must match its GeoNames ID")
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, country_code FROM city WHERE external_id = %s",
                (values["external_id"],),
            )
            existing = cursor.fetchone()
        if existing:
            if existing[1] != country:
                raise ValueError("external city identity conflicts with its stored country")
            return {"status": "existing_city", "city_id": existing[0], "country_code": country, "raw_value_type": raw_type, "source_url": source_url, "evidence": evidence}
        return {
            "status": status,
            "country_code": country,
            "external_source": CITY_EXTERNAL_SOURCE,
            "raw_value_type": raw_type,
            "source_url": source_url,
            "evidence": evidence,
            **values,
        }
    except (KeyError, TypeError, ValueError) as error:
        logger.warning(
            "Ignoring invalid location resolution",
            extra={
                "event": "programme_location_resolution_invalid",
                "concert_id": concert.id,
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )
        return None


def _json_event_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, time)):
        return value.isoformat(timespec="minutes") if isinstance(value, time) else value.isoformat()
    return str(value)


def _validate_composer(cursor, composer: dict[str, Any]) -> None:
    if not composer["name"].strip():
        raise ValueError("Composer names must not be empty")
    if composer["existing_id"] is not None:
        cursor.execute("SELECT 1 FROM composer WHERE id = %s", (composer["existing_id"],))
        if cursor.fetchone() is None:
            raise ValueError(f"Unknown composer ID {composer['existing_id']}")


def _composer_identity(composer: dict[str, Any]) -> tuple[str, int | str]:
    if composer["existing_id"] is not None:
        return ("id", composer["existing_id"])
    return ("name", normalize(composer["name"]))


def _resolve_composer(cursor, composer: dict[str, Any]) -> int:
    if composer["existing_id"] is not None:
        return composer["existing_id"]
    normalized_name = normalize(composer["name"])
    cursor.execute(
        """
        INSERT INTO composer (name, normalized_name)
        VALUES (%s, %s)
        ON CONFLICT (normalized_name) DO UPDATE SET normalized_name = EXCLUDED.normalized_name
        RETURNING id
        """,
        (composer["name"].strip(), normalized_name),
    )
    return cursor.fetchone()[0]


def _resolve_work(cursor, composer_id: int, work: dict[str, Any]) -> int:
    if work["existing_id"] is not None:
        return work["existing_id"]
    normalized_title = normalize(work["title"])
    cursor.execute(
        """
        INSERT INTO work (composer_id, title, normalized_title, catalogue_number)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (composer_id, normalized_title)
        DO UPDATE SET catalogue_number = COALESCE(work.catalogue_number, EXCLUDED.catalogue_number)
        RETURNING id
        """,
        (composer_id, work["title"].strip(), normalized_title, work["catalogue_number"]),
    )
    return cursor.fetchone()[0]


def _append_distinct(values: list[str], value: str) -> None:
    stripped = value.strip()
    if stripped not in values:
        values.append(stripped)


def replace_catalogue_links(cursor, concert_id: int, result: dict[str, Any]) -> None:
    cursor.execute(
        "DELETE FROM classical_concert_work WHERE classical_concert_id = %s",
        (concert_id,),
    )
    cursor.execute(
        "DELETE FROM classical_concert_composer WHERE classical_concert_id = %s",
        (concert_id,),
    )

    composer_ids = {}
    for composer in result["composers"]:
        composer_id = _resolve_composer(cursor, composer)
        composer_ids[_composer_identity(composer)] = composer_id
        cursor.execute(
            """
            INSERT INTO classical_concert_composer (classical_concert_id, composer_id)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
            """,
            (concert_id, composer_id),
        )

    if result["status"] not in {"complete", "partial"}:
        return

    grouped_works: dict[int, dict[str, list[str]]] = {}
    for entry in result["program"]:
        composer_id = composer_ids[_composer_identity(entry["composer"])]
        work_id = _resolve_work(cursor, composer_id, entry["work"])
        grouped = grouped_works.setdefault(work_id, {"labels": [], "evidence": []})
        _append_distinct(grouped["labels"], entry["programme_label"])
        _append_distinct(grouped["evidence"], entry["evidence"])

    for work_id, grouped in grouped_works.items():
        cursor.execute(
            """
            INSERT INTO classical_concert_work
                (classical_concert_id, work_id, programme_label, source_url, evidence)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (classical_concert_id, work_id) DO UPDATE SET
                programme_label = EXCLUDED.programme_label,
                source_url = EXCLUDED.source_url,
                evidence = EXCLUDED.evidence
            """,
            (
                concert_id,
                work_id,
                "; ".join(grouped["labels"]),
                result["source_url"].strip(),
                "\n".join(grouped["evidence"]),
            ),
        )


def apply_event_updates(
    cursor,
    concert: Concert,
    updates: list[dict[str, Any]],
    model: str,
) -> None:
    for update in updates:
        field = update["field"]
        status_timestamp = (
            ", event_status_updated_at = now()" if field == "event_status" else ""
        )
        cursor.execute(
            f"""
            UPDATE classical_concert
            SET {field} = %s,
                updated_at = now()
                {status_timestamp}
            WHERE id = %s
            """,
            (update["db_value"], concert.id),
        )
        cursor.execute(
            """
            INSERT INTO classical_concert_change
                (classical_concert_id, field_name, old_value, new_value,
                 source_url, evidence, model)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                concert.id,
                field,
                Json(update["old_value"]),
                Json(update["new_value"]),
                update["source_url"],
                update["evidence"],
                model,
            ),
        )


def apply_location_resolution(cursor, concert: Concert, resolution: dict[str, Any], model: str) -> None:
    status = resolution["status"]
    if status == "country_only":
        city_id = concert.city_id
        country = resolution["country_code"]
    elif status == "existing_city":
        city_id = resolution["city_id"]
        country = resolution["country_code"]
    else:
        cursor.execute(
            """
            INSERT INTO city
                (english_name, local_name, country_code, external_source, external_id,
                 source_url, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (external_id) DO UPDATE SET
                external_id = EXCLUDED.external_id
            RETURNING id, country_code
            """,
            (resolution["english_name"], resolution["local_name"], resolution["country_code"],
             CITY_EXTERNAL_SOURCE, resolution["external_id"],
             resolution["source_url"], model),
        )
        city_id, stored_country = cursor.fetchone()
        if stored_country != resolution["country_code"]:
            raise ValueError("external city identity conflicts with its stored country")
        country = stored_country

    normalized_alias = normalize_city_key(concert.city_raw)
    if city_id is not None and normalized_alias is not None:
        alias_kind = (
            "legitimate_name"
            if resolution["raw_value_type"] == "legitimate_name"
            else "extraction_artifact"
        )
        source_scope = None if alias_kind == "legitimate_name" else concert.source
        cursor.execute(
            """
            INSERT INTO city_alias
                (city_id, alias, normalized_alias, alias_kind, source_scope,
                 source_url, created_by)
            SELECT %s, %s, %s, %s, %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM city_alias
                WHERE city_id = %s AND normalized_alias = %s
                  AND source_scope IS NOT DISTINCT FROM %s
            )
            """,
            (city_id, concert.city_raw, normalized_alias, alias_kind,
             source_scope, resolution["source_url"], model,
             city_id, normalized_alias, source_scope),
        )

    changes = []
    if city_id != concert.city_id:
        changes.append(("city_id", concert.city_id, city_id))
    if country != concert.country_code_resolved:
        changes.append(("country_code_resolved", concert.country_code_resolved, country))
    cursor.execute(
        """UPDATE classical_concert
           SET city_id = %s, country_code_resolved = %s, updated_at = now()
           WHERE id = %s""",
        (city_id, country, concert.id),
    )
    for field, old, new in changes:
        cursor.execute(
            """INSERT INTO classical_concert_change
                (classical_concert_id, field_name, old_value, new_value,
                 source_url, evidence, model)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (concert.id, field, Json(old), Json(new), resolution["source_url"],
             resolution["evidence"], model),
        )


def persist_result(
    conn,
    concert: Concert,
    result: dict[str, Any],
    model: str,
    event_updates: list[dict[str, Any]] | None = None,
    location_resolution: dict[str, Any] | None = None,
) -> None:
    status = result["status"]
    completed = status in {"complete", "composer_only", "expired_no_program", "failed"}
    if event_updates is None:
        event_updates = validate_event_updates(conn, concert, result.get("event_updates", []))
    try:
        with conn.cursor() as cursor:
            if status != "page_unavailable":
                cursor.execute(
                    "UPDATE classical_concert SET last_verified_at = now() WHERE id = %s",
                    (concert.id,),
                )
            apply_event_updates(cursor, concert, event_updates, model)
            if location_resolution:
                apply_location_resolution(cursor, concert, location_resolution, model)
            cursor.execute(
                "SELECT status FROM concert_program_analysis WHERE classical_concert_id = %s",
                (concert.id,),
            )
            existing = cursor.fetchone()
            if (
                existing
                and existing[0] == "partial"
                and status in {"ambiguous", "no_program", "page_unavailable"}
            ):
                cursor.execute(
                    """
                    UPDATE concert_program_analysis
                    SET attempts = attempts + 1,
                        last_error = %s,
                        last_attempted_at = now(),
                        completed_at = CASE
                            WHEN attempts + 1 >= %s THEN now()
                            ELSE completed_at
                        END
                    WHERE classical_concert_id = %s
                    """,
                    (
                        f"Retry returned {status}: {result['notes']}",
                        MAX_AUTOMATIC_ATTEMPTS,
                        concert.id,
                    ),
                )
                conn.commit()
                return
            if status in {"complete", "partial", "composer_only"}:
                replace_catalogue_links(cursor, concert.id, result)
            cursor.execute(
                """
                INSERT INTO concert_program_analysis
                    (classical_concert_id, status, attempts, model, raw_result, last_error, last_attempted_at, completed_at)
                VALUES (%s, %s, 1, %s, %s, NULL, now(), CASE WHEN %s THEN now() ELSE NULL END)
                ON CONFLICT (classical_concert_id) DO UPDATE SET
                    status = CASE
                        WHEN EXCLUDED.status = 'page_unavailable'
                         AND concert_program_analysis.attempts + 1 >= %s THEN 'failed'
                        ELSE EXCLUDED.status
                    END,
                    attempts = concert_program_analysis.attempts + 1,
                    model = EXCLUDED.model,
                    raw_result = EXCLUDED.raw_result,
                    last_error = NULL,
                    last_attempted_at = now(),
                    completed_at = CASE
                        WHEN EXCLUDED.status = 'page_unavailable'
                         AND concert_program_analysis.attempts + 1 >= %s THEN now()
                        WHEN EXCLUDED.status = 'no_program'
                         AND concert_program_analysis.attempts + 1 >= %s THEN now()
                        WHEN EXCLUDED.status IN ('partial', 'ambiguous')
                         AND concert_program_analysis.attempts + 1 >= %s THEN now()
                        ELSE EXCLUDED.completed_at
                    END
                """,
                (
                    concert.id,
                    status,
                    model,
                    Json(result, dumps=lambda value: json.dumps(value, ensure_ascii=False)),
                    completed,
                    MAX_AUTOMATIC_ATTEMPTS,
                    MAX_AUTOMATIC_ATTEMPTS,
                    MAX_AUTOMATIC_ATTEMPTS,
                    MAX_AUTOMATIC_ATTEMPTS,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def persist_error(conn, concert: Concert, model: str, error: Exception) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO concert_program_analysis
                (classical_concert_id, status, attempts, model, last_error, last_attempted_at)
            VALUES (%s, 'error', 1, %s, %s, now())
            ON CONFLICT (classical_concert_id) DO UPDATE SET
                status = CASE
                    WHEN concert_program_analysis.status = 'partial' THEN 'partial'
                    WHEN concert_program_analysis.attempts + 1 >= %s THEN 'failed'
                    ELSE 'error'
                END,
                attempts = concert_program_analysis.attempts + 1,
                model = CASE
                    WHEN concert_program_analysis.status = 'partial'
                    THEN concert_program_analysis.model
                    ELSE EXCLUDED.model
                END,
                last_error = EXCLUDED.last_error,
                last_attempted_at = now(),
                completed_at = CASE WHEN concert_program_analysis.attempts + 1 >= %s THEN now() ELSE NULL END
            """,
            (concert.id, model, str(error), MAX_AUTOMATIC_ATTEMPTS, MAX_AUTOMATIC_ATTEMPTS),
        )
    conn.commit()


def acquire_lock(conn) -> bool:
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (ADVISORY_LOCK_NAME,))
        return cursor.fetchone()[0]


def release_lock(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (ADVISORY_LOCK_NAME,))


def write_batch_result(
    path: Path | None,
    *,
    status: str,
    selected_count: int,
    group_count: int,
    failure_count: int | None,
    error: Exception | None = None,
) -> None:
    if path is None:
        return
    payload: dict[str, Any] = {
        "status": status,
        "selected_count": selected_count,
        "group_count": group_count,
        "completed_count": (
            selected_count - failure_count if failure_count is not None else None
        ),
        "failure_count": failure_count,
    }
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["error_message"] = str(error)
        if isinstance(error, CodexAuthRequiredError):
            payload["auth_reason_code"] = error.reason_code
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_and_persist_result(
    concert: Concert,
    result: dict[str, Any],
    model: str,
    commit: bool,
) -> None:
    conn = get_connection()
    try:
        validate_result(conn, concert, result)
        event_updates = validate_event_updates(conn, concert, result["event_updates"])
        location_resolution = validate_location_resolution(
            conn,
            concert,
            result.get("location_resolution", {"status": "not_needed"}),
            page_available=result["status"] != "page_unavailable",
        )
        if commit:
            persist_result(
                conn,
                concert,
                result,
                model,
                event_updates,
                location_resolution,
            )
    finally:
        conn.close()


def persist_concert_error(concert: Concert, model: str, error: Exception) -> None:
    conn = get_connection()
    try:
        persist_error(conn, concert, model, error)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def analyze_concert_group(
    codex: AsyncCodex,
    semaphore: asyncio.Semaphore,
    group: ConcertGroup,
    model: str,
    commit: bool,
    timeout_seconds: int,
) -> int:
    async with semaphore:
        started_at = monotonic()
        concert_ids = [concert.id for concert in group.concerts]
        logger.info(
            "Analyzing candidate concert programme group",
            extra={
                "event": "programme_analysis_group_started",
                "concert_ids": concert_ids,
                "group_size": len(group.concerts),
                "group_source": group.concerts[0].source,
                "group_title": group.key[1],
                "group_source_url": group.concerts[0].source_url,
            },
        )
        try:
            group_result = await run_agent(codex, group, model, timeout_seconds)
            expanded = expand_group_result(group, group_result)
        except (asyncio.CancelledError, CodexClientUnhealthyError):
            raise
        except Exception as error:
            raise_for_codex_auth(error)
            logger.exception(
                "Candidate concert programme group failed",
                extra={
                    "event": "programme_analysis_group_failed",
                    "concert_ids": concert_ids,
                    "group_size": len(group.concerts),
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                    "duration_seconds": round(monotonic() - started_at, 3),
                },
            )
            if commit:
                for concert in group.concerts:
                    try:
                        persist_concert_error(concert, model, error)
                    except Exception as persistence_error:
                        logger.exception(
                            "Could not persist concert programme analysis failure",
                            extra={
                                "event": "programme_analysis_error_persistence_failed",
                                "concert_id": concert.id,
                                "error_type": type(persistence_error).__name__,
                                "error_message": str(persistence_error),
                            },
                        )
            return len(group.concerts)

        failures = 0
        subgroup_count = len(group_result["programme_groups"])
        for concert, result in expanded:
            try:
                validate_and_persist_result(concert, result, model, commit)
            except Exception as error:
                failures += 1
                logger.exception(
                    "Concert result from candidate group failed",
                    extra={
                        "event": "programme_analysis_failed",
                        "concert_id": concert.id,
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                    },
                )
                if commit:
                    try:
                        persist_concert_error(concert, model, error)
                    except Exception as persistence_error:
                        logger.exception(
                            "Could not persist concert programme analysis failure",
                            extra={
                                "event": "programme_analysis_error_persistence_failed",
                                "concert_id": concert.id,
                                "error_type": type(persistence_error).__name__,
                                "error_message": str(persistence_error),
                            },
                        )
                continue
            logger.info(
                "Concert programme analysis completed",
                extra={
                    "event": "programme_analysis_completed",
                    "concert_id": concert.id,
                    "status": result["status"],
                    "composer_count": len(result.get("composers") or []),
                    "unresolved_count": len(result.get("unresolved_program") or []),
                    "commit": commit,
                    "duration_seconds": round(monotonic() - started_at, 3),
                    **({} if commit else {"analysis_result": result}),
                },
            )
            if not commit:
                logger.info(
                    "Dry run; no database changes made",
                    extra={
                        "event": "programme_analysis_dry_run",
                        "concert_id": concert.id,
                    },
                )
        logger.info(
            "Candidate concert programme group completed",
            extra={
                "event": "programme_analysis_group_completed",
                "concert_ids": concert_ids,
                "group_size": len(group.concerts),
                "subgroup_count": subgroup_count,
                "completed_count": len(group.concerts) - failures,
                "failure_count": failures,
                "commit": commit,
                "duration_seconds": round(monotonic() - started_at, 3),
            },
        )
        return failures


async def run_concert_groups(
    groups: list[ConcertGroup],
    *,
    model: str,
    commit: bool,
    timeout_seconds: int,
    concurrency: int,
    heartbeat_path: Path | None = None,
) -> int:
    started_at = monotonic()
    logger.info(
        "Starting concert programme analysis batch",
        extra={
            "event": "programme_analysis_batch_started",
            "selected_count": sum(len(group.concerts) for group in groups),
            "group_count": len(groups),
            "concurrency": concurrency,
            "commit": commit,
        },
    )
    heartbeat = lambda: heartbeat_path.touch() if heartbeat_path is not None else None
    heartbeat()
    semaphore = asyncio.Semaphore(concurrency)

    async def run_group(group: ConcertGroup) -> int:
        try:
            return await analyze_concert_group(
                codex,
                semaphore,
                group,
                model,
                commit,
                timeout_seconds,
            )
        finally:
            heartbeat()

    async with AsyncCodex(
        CodexConfig(codex_bin=os.getenv("CODEX_BIN"), cwd=str(Path.cwd()))
    ) as codex:
        await validate_model(codex, model)
        tasks = [asyncio.create_task(run_group(group)) for group in groups]
        try:
            results = await asyncio.gather(*tasks)
        except (CodexClientUnhealthyError, CodexAuthRequiredError):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    failures = sum(results)
    selected_count = sum(len(group.concerts) for group in groups)
    logger.info(
        "Concert programme analysis batch completed",
        extra={
            "event": "programme_analysis_batch_completed",
            "selected_count": selected_count,
            "group_count": len(groups),
            "completed_count": selected_count - failures,
            "failure_count": failures,
            "concurrency": concurrency,
            "commit": commit,
            "duration_seconds": round(monotonic() - started_at, 3),
        },
    )
    return failures


def run(
    *,
    concert_ids: list[int] | None = None,
    limit: int | None = DEFAULT_LIMIT,
    concurrency: int = DEFAULT_CONCURRENCY,
    model: str = DEFAULT_MODEL,
    commit: bool = False,
    force: bool = False,
    unresolved_locations: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    heartbeat_path: Path | None = None,
    result_path: Path | None = None,
) -> int:
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, got {concurrency}")
    conn = get_connection()
    locked = False
    selected_count = 0
    group_count = 0
    try:
        if commit:
            locked = acquire_lock(conn)
            if not locked:
                raise RuntimeError("Another committed concert programme analysis is already running")
            expire_old_no_program(conn)
        concerts = select_concerts(conn, concert_ids, limit, force, unresolved_locations)
        selected_count = len(concerts)
        if not concerts:
            logger.info(
                "No concerts eligible for programme analysis",
                extra={"event": "programme_queue_empty"},
            )
            write_batch_result(
                result_path,
                status="empty",
                selected_count=0,
                group_count=0,
                failure_count=0,
            )
            return 0
        groups = group_concerts(concerts)
        group_count = len(groups)
        failures = asyncio.run(
            run_concert_groups(
                groups,
                model=model,
                commit=commit,
                timeout_seconds=timeout_seconds,
                concurrency=concurrency,
                heartbeat_path=heartbeat_path,
            )
        )
        write_batch_result(
            result_path,
            status="completed",
            selected_count=selected_count,
            group_count=group_count,
            failure_count=failures,
        )
        return failures
    except CodexAuthRequiredError as error:
        logger.critical(
            "Codex authentication failure stopped programme analysis",
            extra={
                "event": "codex_auth_detected",
                "component": "programme-analyzer",
                "reason_code": error.reason_code,
            },
        )
        write_batch_result(
            result_path,
            status="auth_required",
            selected_count=selected_count,
            group_count=group_count,
            failure_count=None,
            error=error,
        )
        raise
    except Exception as error:
        write_batch_result(
            result_path,
            status="fatal",
            selected_count=selected_count,
            group_count=group_count,
            failure_count=None,
            error=error,
        )
        raise
    finally:
        if locked:
            release_lock(conn)
        conn.close()


def scheduled_main() -> None:
    configure_logging()
    failures = run(commit=True, concurrency=resolve_concurrency())
    if failures:
        raise RuntimeError(f"{failures} concert programme analyses failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract composers and works with Codex.")
    parser.add_argument("--concert-id", type=int, action="append", dest="concert_ids")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Maximum concerts to analyze (default: all currently eligible concerts)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help=(
            "Maximum simultaneous candidate-group analyses "
            f"(default: CONCERT_PROGRAM_CONCURRENCY or {DEFAULT_CONCURRENCY})"
        ),
    )
    parser.add_argument("--model", default=os.getenv("CONCERT_PROGRAM_CODEX_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--heartbeat-path", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--result-path", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--unresolved-locations",
        action="store_true",
        help="Re-run upcoming concerts whose raw city has no resolved city ID.",
    )
    return parser.parse_args()


def main() -> None:
    configure_logging()
    args = parse_args()
    try:
        concurrency = resolve_concurrency(args.concurrency)
    except ValueError as error:
        raise SystemExit(f"Invalid concert programme concurrency: {error}") from error
    try:
        failures = run(
            concert_ids=args.concert_ids,
            limit=args.limit,
            concurrency=concurrency,
            model=args.model,
            commit=args.commit,
            force=args.force,
            unresolved_locations=args.unresolved_locations,
            timeout_seconds=args.timeout_seconds,
            heartbeat_path=args.heartbeat_path,
            result_path=args.result_path,
        )
    except Exception as error:
        write_batch_result(
            args.result_path,
            status="fatal",
            selected_count=0,
            group_count=0,
            failure_count=None,
            error=error,
        )
        raise
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
