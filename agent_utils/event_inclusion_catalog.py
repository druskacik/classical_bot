#!/usr/bin/env python3
"""Read-only reporting for event inclusion assessments and quarantine."""

from __future__ import annotations

import argparse
import json
from typing import Any

from agent_utils.search_db import get_connection


def rows_as_dicts(cursor) -> list[dict[str, Any]]:
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def summary(*, prod: bool = False) -> dict[str, Any]:
    with get_connection(prod=prod) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT inclusion_status, COUNT(*) AS concerts
            FROM classical_concert
            GROUP BY inclusion_status
            ORDER BY inclusion_status
            """
        )
        statuses = rows_as_dicts(cursor)
        cursor.execute(
            """
            SELECT origin, decision, COUNT(*) AS assessments
            FROM event_inclusion_assessment
            GROUP BY origin, decision
            ORDER BY origin, decision
            """
        )
        assessments = rows_as_dicts(cursor)
    return {"concerts": statuses, "assessments": assessments}


def list_quarantined(*, prod: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    with get_connection(prod=prod) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.id, c.title, c.date, c.source, c.url,
                   a.origin, a.decision, a.category, a.rationale,
                   a.evidence_urls, a.model, a.created_at
            FROM classical_concert c
            LEFT JOIN LATERAL (
                SELECT e.origin, e.decision, e.category, e.rationale,
                       e.evidence_urls, e.model, e.created_at
                FROM event_inclusion_assessment e
                WHERE e.classical_concert_id = c.id
                ORDER BY e.id DESC
                LIMIT 1
            ) a ON true
            WHERE c.inclusion_status = 'quarantined'
            ORDER BY a.created_at DESC NULLS LAST, c.id
            LIMIT %s
            """,
            (limit,),
        )
        return rows_as_dicts(cursor)


def list_conflicts(*, prod: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    with get_connection(prod=prod) as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.id, c.title, c.date, c.source, c.url,
                   array_agg(DISTINCT e.decision ORDER BY e.decision) AS decisions,
                   COUNT(*) AS assessment_count,
                   MAX(e.created_at) AS latest_assessment_at
            FROM classical_concert c
            JOIN event_inclusion_assessment e ON e.classical_concert_id = c.id
            GROUP BY c.id, c.title, c.date, c.source, c.url
            HAVING BOOL_OR(e.decision = 'classical')
               AND BOOL_OR(e.decision IN ('nonclassical', 'not_event'))
            ORDER BY latest_assessment_at DESC, c.id
            LIMIT %s
            """,
            (limit,),
        )
        return rows_as_dicts(cursor)


def list_assessments(
    *,
    prod: bool = False,
    origin: str | None = None,
    decision: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = ["(%s IS NULL OR e.origin = %s)", "(%s IS NULL OR e.decision = %s)"]
    params: list[Any] = [origin, origin, decision, decision, limit]
    with get_connection(prod=prod) as conn, conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT e.id, e.potential_event_id, e.classical_concert_id,
                   e.origin, e.decision, e.category, e.rationale,
                   e.evidence_urls, e.source_url, e.model, e.created_at
            FROM event_inclusion_assessment e
            WHERE {' AND '.join(clauses)}
            ORDER BY e.id DESC
            LIMIT %s
            """,
            params,
        )
        return rows_as_dicts(cursor)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prod", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("summary")
    for name in ("quarantined", "conflicts"):
        command = subparsers.add_parser(name)
        command.add_argument("--limit", type=int, default=100)
    assessments = subparsers.add_parser("assessments")
    assessments.add_argument("--origin")
    assessments.add_argument("--decision")
    assessments.add_argument("--limit", type=int, default=100)
    return parser.parse_args()


def json_default(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def main() -> None:
    args = parse_args()
    limit = max(1, min(getattr(args, "limit", 100), 1000))
    if args.command == "summary":
        result = summary(prod=args.prod)
    elif args.command == "quarantined":
        result = list_quarantined(prod=args.prod, limit=limit)
    elif args.command == "conflicts":
        result = list_conflicts(prod=args.prod, limit=limit)
    else:
        result = list_assessments(
            prod=args.prod,
            origin=args.origin,
            decision=args.decision,
            limit=limit,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=json_default))


if __name__ == "__main__":
    main()
