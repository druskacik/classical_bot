from __future__ import annotations

import os

import psycopg2
from psycopg2.extras import RealDictCursor


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


class CrawlerRuntimeRegistry:
    def __init__(self, connection=None, *, connection_factory=None) -> None:
        self._connection_factory = connection_factory or get_connection
        self.connection = connection or self._connection_factory()
        self._owns_connection = connection is None

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def reconcile_expired(self) -> int:
        with self.connection, self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE crawler_runtime_attempt AS attempt
                SET outcome = 'interrupted', finished_at = now()
                FROM crawler_runtime_state AS state
                WHERE attempt.crawler_path = state.crawler_path
                  AND attempt.outcome = 'running'
                  AND state.lease_expires_at < now()
                """
            )
            count = cursor.rowcount
            cursor.execute(
                """
                UPDATE crawler_runtime_state
                SET last_attempt_finished_at = now(), last_outcome = 'interrupted',
                    consecutive_failures = consecutive_failures + 1,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE lease_expires_at < now()
                """
            )
        return count

    def claim(
        self,
        crawler_paths: list[str],
        *,
        limit: int,
        worker_id: str,
        lease_seconds: int,
    ) -> list[dict]:
        if not crawler_paths:
            return []
        with self.connection, self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                INSERT INTO crawler_runtime_state (crawler_path)
                SELECT unnest(%s::text[])
                ON CONFLICT (crawler_path) DO NOTHING
                """,
                (crawler_paths,),
            )
            cursor.execute(
                """
                SELECT crawler_path
                FROM crawler_runtime_state
                WHERE crawler_path = ANY(%s::text[])
                  AND (lease_expires_at IS NULL OR lease_expires_at < now())
                ORDER BY last_attempt_started_at ASC NULLS FIRST, crawler_path
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (crawler_paths, limit),
            )
            paths = [row["crawler_path"] for row in cursor.fetchall()]
            claimed = []
            for path in paths:
                cursor.execute(
                    """
                    UPDATE crawler_runtime_state
                    SET last_attempt_started_at = now(), last_attempt_finished_at = NULL,
                        last_outcome = 'running', lease_owner = %s,
                        lease_expires_at = now() + (%s * interval '1 second')
                    WHERE crawler_path = %s
                    """,
                    (worker_id, lease_seconds, path),
                )
                cursor.execute(
                    """
                    INSERT INTO crawler_runtime_attempt (crawler_path, worker_id)
                    VALUES (%s, %s)
                    RETURNING id, crawler_path, started_at
                    """,
                    (path, worker_id),
                )
                claimed.append(dict(cursor.fetchone()))
        return claimed

    def finish(self, attempt_id: int, outcome: str, return_code: int | None) -> None:
        with self.connection, self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE crawler_runtime_attempt
                SET outcome = %s, return_code = %s, finished_at = now()
                WHERE id = %s AND outcome = 'running'
                RETURNING crawler_path, worker_id
                """,
                (outcome, return_code, attempt_id),
            )
            row = cursor.fetchone()
            if row is None:
                return
            path, worker_id = row
            cursor.execute(
                """
                UPDATE crawler_runtime_state
                SET last_attempt_finished_at = now(), last_outcome = %s,
                    last_success_at = CASE WHEN %s = 'succeeded' THEN now() ELSE last_success_at END,
                    consecutive_failures = CASE WHEN %s = 'succeeded' THEN 0 ELSE consecutive_failures + 1 END,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE crawler_path = %s AND lease_owner = %s
                """,
                (outcome, outcome, outcome, path, worker_id),
            )

    def cleanup(self, retention_days: int) -> int:
        with self.connection, self.connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM crawler_runtime_attempt
                WHERE finished_at < now() - (%s * interval '1 day')
                """,
                (retention_days,),
            )
            return cursor.rowcount
