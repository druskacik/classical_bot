from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2.extras import Json, RealDictCursor


logger = logging.getLogger(__name__)
DUE_STATUSES = ("pending", "retry_wait", "blocked")
GEOGRAPHIC_SCOPES = {"unknown", "country", "multi_country"}
PROTECTED_IDENTITY_STATUSES = {
    "processing", "pr_open", "active", "blocked", "duplicate", "disabled"
}


def identity_is_mutable(source: dict) -> bool:
    return (
        source.get("geographic_scope", "unknown") == "unknown"
        and source["status"] not in PROTECTED_IDENTITY_STATUSES
    )


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


def normalize_source_url(url: str) -> str:
    value = url.strip()
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Source URL must be an HTTP(S) URL: {url!r}")
    host = parsed.hostname.lower().encode("idna").decode("ascii")
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    netloc = host
    if port and port not in {80, 443}:
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, parsed.query, ""))


def normalized_crawler_path(path: str | Path) -> str:
    value = Path(path)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError("crawler_path must be a repository-relative path")
    if len(value.parts) != 3 or value.parts[0] != "crawlers":
        raise ValueError("crawler_path must use crawlers/<country>/<slug>")
    return value.as_posix()


def normalized_geographic_identity(
    country_code: str | None,
    geographic_scope: str,
    crawler_path: str | None,
) -> tuple[str | None, str, str | None]:
    scope = geographic_scope.strip().lower()
    if scope not in GEOGRAPHIC_SCOPES:
        raise ValueError("geographic_scope must be unknown, country, or multi_country")
    country = country_code.strip().upper() if country_code else None
    if country is not None and not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country_code must be a two-letter ISO code or null")
    path = normalized_crawler_path(crawler_path) if crawler_path else None
    if scope == "country":
        if country is None or path is None:
            raise ValueError("country scope requires country_code and crawler_path")
        if Path(path).parts[1] != country.lower():
            raise ValueError("country crawler_path must match country_code")
    elif scope == "multi_country":
        if country is not None or path is None or Path(path).parts[1] != "common":
            raise ValueError(
                "multi_country scope requires a null country and crawlers/common/<slug>"
            )
    return country, scope, path


class CrawlerRegistry:
    def __init__(self, connection=None, *, connection_factory=None) -> None:
        self._connection_factory = connection_factory or get_connection
        self.connection = (
            connection if connection is not None else self._connection_factory()
        )
        self._owns_connection = connection is None

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> CrawlerRegistry:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _reconnect(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass
        self.connection = self._connection_factory()
        logger.warning(
            "Reconnected crawler-factory database connection",
            extra={
                "event": "factory_database_reconnected",
                "component": "crawler-factory",
            },
        )

    def _open_cursor(self):
        if self._owns_connection and self.connection.closed:
            self._reconnect()
        try:
            return self.connection.cursor(cursor_factory=RealDictCursor)
        except (psycopg2.InterfaceError, psycopg2.OperationalError):
            if not self._owns_connection:
                raise
            self._reconnect()
            return self.connection.cursor(cursor_factory=RealDictCursor)

    @contextmanager
    def cursor(self) -> Iterator:
        with self._open_cursor() as cursor:
            yield cursor

    def preflight(self) -> int:
        with self.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM crawler_source")
            count = cursor.fetchone()["count"]
        if count == 0:
            raise RuntimeError(
                "crawler_source is empty; apply a seed before running the crawler factory"
            )
        return count

    def ingest_source(
        self,
        url: str,
        country_code: str | None,
        *,
        canonical_url: str | None = None,
        crawler_path: str | None = None,
        priority: int = 0,
        discovered_by: str = "manual",
        metadata: dict | None = None,
        geographic_scope: str = "unknown",
        commit: bool = True,
    ) -> dict:
        normalized = normalize_source_url(url)
        preferred = canonical_url or url
        country, scope, path = normalized_geographic_identity(
            country_code, geographic_scope, crawler_path
        )
        try:
            with self.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cs.*
                    FROM crawler_source_url AS u
                    JOIN crawler_source AS cs ON cs.id = u.crawler_source_id
                    WHERE u.normalized_url = %s
                    """,
                    (normalized,),
                )
                existing = cursor.fetchone()
                if existing:
                    cursor.execute(
                        """
                        UPDATE crawler_source_url
                        SET last_seen_at = now()
                        WHERE normalized_url = %s
                        """,
                        (normalized,),
                    )
                    if identity_is_mutable(existing):
                        cursor.execute(
                            """
                            UPDATE crawler_source
                            SET country_code = %s, geographic_scope = %s,
                                canonical_url = %s, crawler_path = %s,
                                priority = GREATEST(priority, %s), updated_at = now()
                            WHERE id = %s
                            RETURNING *
                            """,
                            (country, scope, preferred, path, priority, existing["id"]),
                        )
                        existing = cursor.fetchone()
                    if commit:
                        self.connection.commit()
                    return dict(existing)
                if path:
                    cursor.execute(
                        "SELECT * FROM crawler_source WHERE crawler_path = %s",
                        (path,),
                    )
                    path_owner = cursor.fetchone()
                    if path_owner:
                        self._record_alias(
                            cursor,
                            path_owner["id"],
                            url,
                            "submitted",
                            discovered_by,
                        )
                        if canonical_url and normalize_source_url(canonical_url) != normalized:
                            self._record_alias(
                                cursor,
                                path_owner["id"],
                                canonical_url,
                                "canonical",
                                discovered_by,
                            )
                        if commit:
                            self.connection.commit()
                        return dict(path_owner)
                cursor.execute(
                    """
                    INSERT INTO crawler_source (
                        country_code, geographic_scope, canonical_url,
                        crawler_path, status, priority
                    )
                    VALUES (%s, %s, %s, %s, 'pending', %s)
                    RETURNING *
                    """,
                    (country, scope, preferred, path, priority),
                )
                source = dict(cursor.fetchone())
                cursor.execute(
                    """
                    INSERT INTO crawler_source_url (
                        crawler_source_id, url, normalized_url, role,
                        discovered_by, metadata_json
                    )
                    VALUES (%s, %s, %s, 'submitted', %s, %s)
                    """,
                    (source["id"], url, normalized, discovered_by, Json(metadata or {})),
                )
                if canonical_url and normalize_source_url(canonical_url) != normalized:
                    self._record_alias(
                        cursor,
                        source["id"],
                        canonical_url,
                        "canonical",
                        discovered_by,
                    )
            if commit:
                self.connection.commit()
            return source
        except Exception:
            self.connection.rollback()
            raise

    def _record_alias(
        self,
        cursor,
        source_id: int,
        url: str,
        role: str,
        discovered_by: str,
    ) -> int:
        normalized = normalize_source_url(url)
        cursor.execute(
            """
            INSERT INTO crawler_source_url (
                crawler_source_id, url, normalized_url, role, discovered_by
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (normalized_url) DO UPDATE
            SET last_seen_at = now()
            RETURNING crawler_source_id
            """,
            (source_id, url, normalized, role, discovered_by),
        )
        return cursor.fetchone()["crawler_source_id"]

    def create_run(self, run_id: str, worker_id: str, branch: str, model: str) -> None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO crawler_factory_run (id, worker_id, branch, model)
                VALUES (%s, %s, %s, %s)
                """,
                (run_id, worker_id, branch, model),
            )
        self.connection.commit()

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        pull_request_url: str | None = None,
    ) -> None:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE crawler_factory_run
                SET status = %s, pull_request_url = COALESCE(%s, pull_request_url),
                    finished_at = now()
                WHERE id = %s
                """,
                (status, pull_request_url, run_id),
            )
        self.connection.commit()

    def recover_expired_leases(self) -> int:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE crawler_source
                SET status = 'retry_wait', next_attempt_at = now(),
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE status = 'processing' AND lease_expires_at < now()
                """,
            )
            recovered = cursor.rowcount
            cursor.execute(
                """
                UPDATE crawler_source_attempt
                SET outcome = 'abandoned', finished_at = now(),
                    error = COALESCE(error, 'worker lease expired')
                WHERE outcome = 'running'
                  AND crawler_source_id IN (
                      SELECT id FROM crawler_source
                      WHERE status = 'retry_wait' AND next_attempt_at <= now()
                  )
                """
            )
        self.connection.commit()
        return recovered

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_minutes: int,
        source_ids: list[int] | None = None,
    ) -> dict | None:
        id_filter = "AND id = ANY(%s)" if source_ids else ""
        params: list[object] = [list(DUE_STATUSES)]
        if source_ids:
            params.append(source_ids)
        params.extend([worker_id, lease_minutes])
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                WITH candidate AS (
                    SELECT id
                    FROM crawler_source
                    WHERE status = ANY(%s)
                      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                      {id_filter}
                    ORDER BY priority DESC, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE crawler_source AS source
                SET status = 'processing', lease_owner = %s,
                    lease_expires_at = now() + (%s * interval '1 minute'),
                    updated_at = now()
                FROM candidate
                WHERE source.id = candidate.id
                RETURNING source.*
                """,
                params,
            )
            source = cursor.fetchone()
        self.connection.commit()
        return dict(source) if source else None

    def start_attempt(self, source: dict, run_id: str) -> int:
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO crawler_source_attempt (
                    crawler_source_id, run_id, attempted_url, crawler_path
                )
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (source["id"], run_id, source["canonical_url"], source["crawler_path"]),
            )
            attempt_id = cursor.fetchone()["id"]
        self.connection.commit()
        return attempt_id

    def assign_resolved_identity(
        self,
        source_id: int,
        resolved_url: str,
        crawler_path: str,
        *,
        country_code: str | None = None,
        geographic_scope: str | None = None,
    ) -> dict:
        path = normalized_crawler_path(crawler_path)
        if geographic_scope is None:
            geographic_scope = (
                "multi_country" if Path(path).parts[1] == "common" else "country"
            )
        if geographic_scope == "country" and country_code is None:
            country_code = Path(path).parts[1].upper()
        country, scope, path = normalized_geographic_identity(
            country_code, geographic_scope, path
        )
        try:
            with self.cursor() as cursor:
                alias_owner = self._record_alias(
                    cursor,
                    source_id,
                    resolved_url,
                    "redirect",
                    "http_redirect",
                )
                cursor.execute(
                    "SELECT id FROM crawler_source WHERE crawler_path = %s",
                    (path,),
                )
                path_owner_row = cursor.fetchone()
                path_owner = path_owner_row["id"] if path_owner_row else None
                duplicate_of = next(
                    (
                        owner
                        for owner in (alias_owner, path_owner)
                        if owner is not None and owner != source_id
                    ),
                    None,
                )
                if duplicate_of:
                    cursor.execute(
                        """
                        UPDATE crawler_source
                        SET status = 'duplicate', duplicate_of_id = %s,
                            lease_owner = NULL, lease_expires_at = NULL,
                            updated_at = now()
                        WHERE id = %s
                        RETURNING *
                        """,
                        (duplicate_of, source_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE crawler_source
                        SET canonical_url = %s, country_code = %s,
                            geographic_scope = %s, crawler_path = %s, updated_at = now()
                        WHERE id = %s
                        RETURNING *
                        """,
                        (resolved_url, country, scope, path, source_id),
                    )
                source = dict(cursor.fetchone())
            self.connection.commit()
            return source
        except Exception:
            self.connection.rollback()
            raise

    def complete_attempt(
        self,
        source_id: int,
        attempt_id: int,
        outcome: str,
        *,
        resolved_url: str | None = None,
        crawler_path: str | None = None,
        commit_sha: str | None = None,
        warning: str | None = None,
        error: str | None = None,
    ) -> None:
        retry_after = None
        source_status = "processing"
        if outcome == "generation_failed":
            source_status = "retry_wait"
            retry_after = datetime.now(UTC) + timedelta(days=7)
        elif outcome == "blocked":
            retry_after = datetime.now(UTC) + timedelta(days=30)
        elif outcome == "duplicate":
            source_status = "duplicate"
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE crawler_source_attempt
                SET outcome = %s, resolved_url = %s,
                    crawler_path = COALESCE(%s, crawler_path), commit_sha = %s,
                    generation_warning = %s, error = %s, retry_after = %s,
                    finished_at = now()
                WHERE id = %s
                """,
                (
                    outcome,
                    resolved_url,
                    crawler_path,
                    commit_sha,
                    warning,
                    error,
                    retry_after,
                    attempt_id,
                ),
            )
            cursor.execute(
                """
                UPDATE crawler_source
                SET status = %s, next_attempt_at = %s,
                    lease_owner = CASE WHEN %s = 'processing' THEN lease_owner ELSE NULL END,
                    lease_expires_at = CASE
                        WHEN %s = 'processing' THEN lease_expires_at ELSE NULL
                    END,
                    updated_at = now()
                WHERE id = %s
                """,
                (source_status, retry_after, source_status, source_status, source_id),
            )
        self.connection.commit()

    def abandon_attempt(
        self,
        source_id: int,
        attempt_id: int,
        *,
        error: str,
    ) -> None:
        try:
            with self.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE crawler_source_attempt
                    SET outcome = 'abandoned', error = %s, retry_after = now(),
                        finished_at = now()
                    WHERE id = %s AND crawler_source_id = %s AND outcome = 'running'
                    """,
                    (error, attempt_id, source_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("expected one running crawler attempt to abandon")
                cursor.execute(
                    """
                    UPDATE crawler_source
                    SET status = 'retry_wait', next_attempt_at = now(),
                        lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                    WHERE id = %s AND status = 'processing'
                    """,
                    (source_id,),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("expected one processing crawler source to release")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def mark_pr_open(self, run_id: str, source_ids: list[int], pr_url: str) -> None:
        if not source_ids:
            return
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE crawler_source
                SET status = 'pr_open', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = now()
                WHERE id = ANY(%s)
                """,
                (source_ids,),
            )
            cursor.execute(
                """
                UPDATE crawler_source_attempt
                SET pull_request_url = %s
                WHERE run_id = %s AND crawler_source_id = ANY(%s)
                """,
                (pr_url, run_id, source_ids),
            )
        self.connection.commit()

    def reconcile_workspace(self, workspace: Path) -> dict[str, int]:
        counts = {"active": 0, "blocked": 0}
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT source.id, source.crawler_path, latest.retry_after
                FROM crawler_source AS source
                LEFT JOIN LATERAL (
                    SELECT retry_after
                    FROM crawler_source_attempt
                    WHERE crawler_source_id = source.id
                    ORDER BY id DESC
                    LIMIT 1
                ) AS latest ON true
                WHERE source.status = 'pr_open' AND source.crawler_path IS NOT NULL
                """
            )
            sources = list(cursor.fetchall())
            for source in sources:
                directory = workspace / source["crawler_path"]
                if (directory / "main.py").exists():
                    status = "active"
                    retry_at = None
                elif (directory / "BLOCKED.md").exists():
                    status = "blocked"
                    retry_at = (
                        source["retry_after"]
                        or datetime.now(UTC) + timedelta(days=30)
                    )
                else:
                    continue
                cursor.execute(
                    """
                    UPDATE crawler_source
                    SET status = %s, next_attempt_at = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (status, retry_at, source["id"]),
                )
                counts[status] += 1
        self.connection.commit()
        return counts

    def pr_open_sources(self) -> list[dict]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT cs.id, cs.crawler_path, latest.pull_request_url
                FROM crawler_source AS cs
                JOIN LATERAL (
                    SELECT pull_request_url
                    FROM crawler_source_attempt
                    WHERE crawler_source_id = cs.id
                      AND pull_request_url IS NOT NULL
                    ORDER BY id DESC
                    LIMIT 1
                ) AS latest ON true
                WHERE cs.status = 'pr_open'
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def transition_sources(
        self,
        source_ids: list[int],
        status: str,
        *,
        retry_after: datetime | None = None,
    ) -> None:
        if not source_ids:
            return
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE crawler_source
                SET status = %s, next_attempt_at = %s,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id = ANY(%s)
                """,
                (status, retry_after, source_ids),
            )
        self.connection.commit()

    def reconcile_run_statuses(self) -> int:
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE crawler_factory_run AS run
                SET status = CASE
                        WHEN NOT EXISTS (
                            SELECT 1
                            FROM crawler_source_attempt AS attempt
                            JOIN crawler_source AS source
                              ON source.id = attempt.crawler_source_id
                            WHERE attempt.run_id = run.id
                              AND source.status NOT IN ('active', 'blocked', 'duplicate')
                        )
                        THEN 'completed'
                        ELSE 'failed'
                    END,
                    finished_at = now()
                WHERE run.status = 'pr_open'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM crawler_source_attempt AS attempt
                      JOIN crawler_source AS source
                        ON source.id = attempt.crawler_source_id
                      WHERE attempt.run_id = run.id
                        AND source.status = 'pr_open'
                  )
                """
            )
            updated = cursor.rowcount
        self.connection.commit()
        return updated
