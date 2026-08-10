#!/usr/bin/env python3
"""Read-only lookup commands for the potential-event classification agent."""

from __future__ import annotations

import argparse
import json
from typing import Any

from agent_utils.search_db import get_connection


EVENT_COLUMNS = """
    id, title, date, url, source, source_url, time_from, time_to,
    city_raw, country_code_raw, venue, type, description
"""


def json_default(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def source_filter(source_url: str | None) -> tuple[str, list[Any]]:
    if source_url is None:
        return "", []
    return " AND source_url IS NOT DISTINCT FROM %s", [source_url]


def source_summary(source: str, source_url: str | None) -> dict[str, Any]:
    url_sql, url_params = source_filter(source_url)
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE analyzed = false),
                   COUNT(*) FILTER (WHERE analyzed = true AND is_classical_concert = true),
                   COUNT(*) FILTER (WHERE analyzed = true AND is_classical_concert = false),
                   MIN(date), MAX(date), COUNT(DISTINCT title)
            FROM potential_event
            WHERE source IS NOT DISTINCT FROM %s{url_sql}
            """,
            [source, *url_params],
        )
        row = cursor.fetchone()
        cursor.execute(
            """
            SELECT id, crawler_path, canonical_url, status
            FROM crawler_source
            WHERE canonical_url = %s
               OR EXISTS (
                    SELECT 1 FROM crawler_source_url u
                    WHERE u.crawler_source_id = crawler_source.id AND u.url = %s
               )
            ORDER BY CASE WHEN canonical_url = %s THEN 0 ELSE 1 END, id
            LIMIT 5
            """,
            (source_url, source_url, source_url),
        )
        registry = [
            {
                "id": item[0],
                "crawler_path": item[1],
                "canonical_url": item[2],
                "status": item[3],
            }
            for item in cursor.fetchall()
        ] if source_url else []
    return {
        "source": source,
        "source_url": source_url,
        "total": row[0],
        "unclassified": row[1],
        "classical": row[2],
        "nonclassical": row[3],
        "earliest_date": row[4],
        "latest_date": row[5],
        "distinct_titles": row[6],
        "crawler_registry": registry,
    }


def list_events(
    source: str,
    source_url: str | None,
    *,
    limit: int,
    after_id: int | None,
    unclassified_only: bool,
) -> list[dict[str, Any]]:
    url_sql, url_params = source_filter(source_url)
    clauses = ["source IS NOT DISTINCT FROM %s"]
    params: list[Any] = [source, *url_params]
    if url_sql:
        clauses.append("source_url IS NOT DISTINCT FROM %s")
    if after_id is not None:
        clauses.append("id > %s")
        params.append(after_id)
    if unclassified_only:
        clauses.append("analyzed = false")
    params.append(limit)
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {EVENT_COLUMNS} FROM potential_event "
            f"WHERE {' AND '.join(clauses)} ORDER BY id LIMIT %s",
            params,
        )
        names = [description[0] for description in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def get_events(ids: list[int]) -> list[dict[str, Any]]:
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {EVENT_COLUMNS} FROM potential_event WHERE id = ANY(%s) ORDER BY id",
            (ids,),
        )
        names = [description[0] for description in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def search_events(
    source: str,
    source_url: str | None,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    url_sql, url_params = source_filter(source_url)
    with get_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {EVENT_COLUMNS}, analyzed, is_classical_concert
            FROM potential_event
            WHERE source IS NOT DISTINCT FROM %s{url_sql}
              AND (title ILIKE %s OR COALESCE(description, '') ILIKE %s)
            ORDER BY date DESC, id DESC
            LIMIT %s
            """,
            [source, *url_params, f"%{query}%", f"%{query}%", limit],
        )
        names = [description[0] for description in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary = subparsers.add_parser("source-summary")
    summary.add_argument("--source", required=True)
    summary.add_argument("--source-url")

    listing = subparsers.add_parser("list-events")
    listing.add_argument("--source", required=True)
    listing.add_argument("--source-url")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--after-id", type=int)
    listing.add_argument("--all", action="store_true")

    events = subparsers.add_parser("get-events")
    events.add_argument("--ids", type=int, nargs="+", required=True)

    search = subparsers.add_parser("search-events")
    search.add_argument("--source", required=True)
    search.add_argument("--source-url")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=25)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "source-summary":
        result = source_summary(args.source, args.source_url)
    elif args.command == "list-events":
        result = list_events(
            args.source,
            args.source_url,
            limit=max(1, min(args.limit, 500)),
            after_id=args.after_id,
            unclassified_only=not args.all,
        )
    elif args.command == "get-events":
        result = get_events(args.ids)
    else:
        result = search_events(
            args.source,
            args.source_url,
            args.query,
            max(1, min(args.limit, 100)),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=json_default))


if __name__ == "__main__":
    main()
