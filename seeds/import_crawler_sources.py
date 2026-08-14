from __future__ import annotations

import argparse
import csv
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_utils.search_db import load_environment
from automation.crawler_registry import CrawlerRegistry
from build_crawlers_with_codex import crawler_folder_name


REQUIRED_COLUMNS = {"url"}


def expected_crawler_path(url: str, country_code: str) -> str:
    return f"crawlers/{country_code.lower()}/{crawler_folder_name(url)}"


def blocked_source_state(path: Path) -> tuple[str, datetime | None]:
    from automation.run_crawler_factory import blocked_registry_state

    try:
        return blocked_registry_state(path)
    except Exception:
        return "blocked", datetime.now(UTC) + timedelta(days=30)


def import_seed(path: Path, repository_root: Path, registry: CrawlerRegistry) -> dict:
    payload = path.read_bytes()
    checksum = hashlib.sha256(payload).hexdigest()
    with registry.cursor() as cursor:
        cursor.execute(
            "SELECT sha256, row_count FROM crawler_source_seed WHERE filename = %s",
            (path.name,),
        )
        applied = cursor.fetchone()
    if applied:
        if applied["sha256"] != checksum:
            raise RuntimeError(
                f"Seed {path.name} was already applied with a different checksum; "
                "create a new numbered seed file"
            )
        return {"filename": path.name, "status": "already_applied", "rows": applied["row_count"]}

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not REQUIRED_COLUMNS.issubset(reader.fieldnames or []):
            raise ValueError(f"Seed must contain columns: {sorted(REQUIRED_COLUMNS)}")
        rows = list(reader)
    counts = {"active": 0, "blocked": 0, "needs_attention": 0, "pending": 0}
    try:
        for row in rows:
            url = row["url"].strip()
            canonical_url = (row.get("canonical_url") or "").strip() or None
            country = (row.get("country_code") or "").strip().upper() or None
            crawler_path = (row.get("crawler_path") or "").strip()
            scope_hint = (row.get("scope_hint") or "").strip().lower() or "unknown"
            if not crawler_path and scope_hint == "multi_country":
                crawler_path = f"crawlers/common/{crawler_folder_name(canonical_url or url)}"
            elif not crawler_path and country:
                crawler_path = expected_crawler_path(canonical_url or url, country)
            crawler_path = crawler_path or None
            directory = repository_root / crawler_path if crawler_path else None
            has_main = bool(directory and (directory / "main.py").exists())
            has_blocked = bool(directory and (directory / "BLOCKED.md").exists())
            geographic_scope = "unknown"
            if (has_main or has_blocked) and scope_hint == "unknown":
                scope_hint = (
                    "multi_country"
                    if crawler_path and crawler_path.split("/")[1] == "common"
                    else "country"
                )
                if scope_hint == "multi_country":
                    country = None
            if has_main or has_blocked:
                geographic_scope = scope_hint
            source = registry.ingest_source(
                url,
                country,
                canonical_url=canonical_url,
                crawler_path=crawler_path,
                priority=int((row.get("priority") or "0").strip()),
                discovered_by=f"seed:{path.name}",
                metadata={"notes": (row.get("notes") or "").strip()},
                geographic_scope=geographic_scope,
                commit=False,
            )
            effective_path = source.get("crawler_path")
            effective_directory = repository_root / effective_path if effective_path else None
            if effective_directory and (effective_directory / "main.py").exists():
                status = "active"
                next_attempt_at = None
            elif effective_directory and (effective_directory / "BLOCKED.md").exists():
                status, next_attempt_at = blocked_source_state(
                    effective_directory / "BLOCKED.md"
                )
            else:
                status = "pending"
                next_attempt_at = None
            with registry.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE crawler_source
                    SET status = %s, next_attempt_at = %s, updated_at = now()
                    WHERE id = %s
                      AND status NOT IN (
                          'processing', 'pr_open', 'active', 'blocked',
                          'duplicate', 'disabled'
                      )
                    RETURNING status
                    """,
                    (status, next_attempt_at, source["id"]),
                )
                updated = cursor.fetchone()
            effective_status = updated["status"] if updated else source["status"]
            if effective_status in counts:
                counts[effective_status] += 1
        with registry.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO crawler_source_seed (filename, sha256, row_count)
                VALUES (%s, %s, %s)
                """,
                (path.name, checksum, len(rows)),
            )
        registry.connection.commit()
    except Exception:
        registry.connection.rollback()
        raise
    return {"filename": path.name, "status": "applied", "rows": len(rows), **counts}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import a versioned crawler-source seed.")
    parser.add_argument("seed", type=Path)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Load .env.prod before connecting (for an explicit production import).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_environment(prod=args.prod)
    with CrawlerRegistry() as registry:
        result = import_seed(args.seed, args.repository_root.resolve(), registry)
    print(
        f"{result['status']}: {result['filename']} ({result['rows']} rows"
        + (
            f", {result['active']} active, {result['blocked']} blocked, "
            f"{result['pending']} pending)"
            if result["status"] == "applied"
            else ")"
        )
    )


if __name__ == "__main__":
    main()
