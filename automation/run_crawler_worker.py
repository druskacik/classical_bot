from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Callable

from automation.crawler_runtime_registry import CrawlerRuntimeRegistry
from observability import configure_logging


logger = logging.getLogger(__name__)
REPOSITORY_ROOT = Path(__file__).parents[1]
MINIMUM_CRAWL_INTERVAL_SECONDS = 86400
IDLE_INTERVAL_SECONDS = 300


def positive_integer(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < 1:
        raise ValueError(f"{name} must be at least 1")
    return parsed


@dataclass(frozen=True)
class CrawlerWorkerConfig:
    concurrency: int
    timeout_seconds: int
    terminate_grace_seconds: int
    lease_seconds: int
    history_retention_days: int

    @classmethod
    def from_environment(cls) -> "CrawlerWorkerConfig":
        timeout = positive_integer(
            os.getenv("CRAWLER_TIMEOUT_SECONDS", "1800"),
            "CRAWLER_TIMEOUT_SECONDS",
        )
        terminate_grace = positive_integer(
            os.getenv("CRAWLER_TERMINATE_GRACE_SECONDS", "30"),
            "CRAWLER_TERMINATE_GRACE_SECONDS",
        )
        lease = positive_integer(
            os.getenv("CRAWLER_LEASE_SECONDS", str(timeout + 300)),
            "CRAWLER_LEASE_SECONDS",
        )
        if lease < timeout + terminate_grace:
            raise ValueError(
                "CRAWLER_LEASE_SECONDS must cover CRAWLER_TIMEOUT_SECONDS plus "
                "CRAWLER_TERMINATE_GRACE_SECONDS"
            )
        return cls(
            concurrency=positive_integer(
                os.getenv("CRAWLER_CONCURRENCY", "5"), "CRAWLER_CONCURRENCY"
            ),
            timeout_seconds=timeout,
            terminate_grace_seconds=terminate_grace,
            lease_seconds=lease,
            history_retention_days=positive_integer(
                os.getenv("CRAWLER_HISTORY_RETENTION_DAYS", "90"),
                "CRAWLER_HISTORY_RETENTION_DAYS",
            ),
        )


def discover_crawler_paths(root: Path = REPOSITORY_ROOT) -> list[str]:
    return sorted(
        main_file.parent.relative_to(root).as_posix()
        for main_file in (root / "crawlers").glob("*/*/main.py")
    )


def module_for_path(crawler_path: str) -> str:
    return crawler_path.replace("/", ".") + ".main"


@dataclass
class RunningCrawler:
    attempt_id: int
    crawler_path: str
    process: subprocess.Popen
    started_at: float
    termination_started_at: float | None = None
    outcome: str | None = None


class CrawlerWorker:
    def __init__(
        self,
        config: CrawlerWorkerConfig,
        *,
        stop_event: threading.Event | None = None,
        drain_event: threading.Event | None = None,
        before_batch: Callable[[], None] | None = None,
        registry_factory: Callable[[], CrawlerRuntimeRegistry] = CrawlerRuntimeRegistry,
        repository_root: Path = REPOSITORY_ROOT,
        worker_id: str | None = None,
    ) -> None:
        self.config = config
        self.stop_event = stop_event or threading.Event()
        self.drain_event = drain_event or threading.Event()
        self.before_batch = before_batch
        self.registry_factory = registry_factory
        self.repository_root = repository_root
        self.worker_id = worker_id or f"crawler-worker-{uuid.uuid4()}"

    def stop(self) -> None:
        self.stop_event.set()

    def wait(self, seconds: int) -> None:
        deadline = monotonic() + seconds
        while not self.stop_event.is_set() and not self.drain_event.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(1, remaining))

    def _signal(self, running: RunningCrawler, signum: int) -> None:
        try:
            os.killpg(running.process.pid, signum)
        except ProcessLookupError:
            pass

    def _launch(self, claim: dict) -> RunningCrawler:
        path = claim["crawler_path"]
        process = subprocess.Popen(
            [sys.executable, "-m", module_for_path(path)],
            cwd=self.repository_root,
            start_new_session=True,
        )
        logger.info(
            "Crawler subprocess started",
            extra={
                "event": "crawler_worker_process_started",
                "component": "crawler-worker",
                "crawler_path": path,
                "attempt_id": claim["id"],
                "child_pid": process.pid,
            },
        )
        return RunningCrawler(claim["id"], path, process, monotonic())

    def _run_claims(self, registry: CrawlerRuntimeRegistry, claims: list[dict]) -> None:
        active: list[RunningCrawler] = []
        for claim in claims:
            try:
                active.append(self._launch(claim))
            except Exception:
                logger.exception(
                    "Could not launch crawler subprocess",
                    extra={
                        "event": "crawler_worker_launch_failed",
                        "component": "crawler-worker",
                        "crawler_path": claim["crawler_path"],
                        "attempt_id": claim["id"],
                    },
                )
                registry.finish(claim["id"], "launch_failed", None)

        while active:
            now = monotonic()
            for running in list(active):
                return_code = running.process.poll()
                if return_code is not None:
                    outcome = running.outcome or (
                        "succeeded" if return_code == 0 else "failed"
                    )
                    registry.finish(running.attempt_id, outcome, return_code)
                    logger.info(
                        "Crawler subprocess finished",
                        extra={
                            "event": "crawler_worker_process_finished",
                            "component": "crawler-worker",
                            "crawler_path": running.crawler_path,
                            "attempt_id": running.attempt_id,
                            "outcome": outcome,
                            "return_code": return_code,
                            "duration_seconds": round(now - running.started_at, 3),
                        },
                    )
                    active.remove(running)
                    continue

                should_interrupt = self.stop_event.is_set()
                timed_out = now - running.started_at >= self.config.timeout_seconds
                if running.termination_started_at is None and (should_interrupt or timed_out):
                    running.outcome = "interrupted" if should_interrupt else "timed_out"
                    running.termination_started_at = now
                    self._signal(running, signal.SIGTERM)
                    logger.warning(
                        "Terminating crawler subprocess",
                        extra={
                            "event": "crawler_worker_process_terminating",
                            "component": "crawler-worker",
                            "crawler_path": running.crawler_path,
                            "attempt_id": running.attempt_id,
                            "outcome": running.outcome,
                        },
                    )
                elif (
                    running.termination_started_at is not None
                    and now - running.termination_started_at
                    >= self.config.terminate_grace_seconds
                ):
                    self._signal(running, signal.SIGKILL)
            if active:
                self.stop_event.wait(0.25)

    def run(self) -> None:
        crawler_paths = discover_crawler_paths(self.repository_root)
        if not crawler_paths:
            raise RuntimeError("No crawler entrypoints were discovered")
        logger.info(
            "Running continuous crawler worker",
            extra={
                "event": "crawler_worker_started",
                "component": "crawler-worker",
                "crawler_count": len(crawler_paths),
                "concurrency": self.config.concurrency,
                "timeout_seconds": self.config.timeout_seconds,
                "worker_id": self.worker_id,
            },
        )
        with self.registry_factory() as registry:
            registry.reconcile_expired()
            registry.cleanup(self.config.history_retention_days)
            next_cleanup_at = monotonic() + 86400
            while not self.stop_event.is_set() and not self.drain_event.is_set():
                if monotonic() >= next_cleanup_at:
                    registry.cleanup(self.config.history_retention_days)
                    next_cleanup_at = monotonic() + 86400
                if self.before_batch is not None:
                    self.before_batch()
                if self.drain_event.is_set():
                    break
                registry.reconcile_expired()
                claims = registry.claim(
                    crawler_paths,
                    limit=self.config.concurrency,
                    worker_id=self.worker_id,
                    lease_seconds=self.config.lease_seconds,
                    minimum_interval_seconds=MINIMUM_CRAWL_INTERVAL_SECONDS,
                )
                if not claims:
                    logger.info(
                        "Crawler queue has no eligible entries; waiting before recheck",
                        extra={
                            "event": "crawler_worker_queue_idle",
                            "component": "crawler-worker",
                            "idle_interval_seconds": IDLE_INTERVAL_SECONDS,
                            "minimum_crawl_interval_seconds": MINIMUM_CRAWL_INTERVAL_SECONDS,
                        },
                    )
                    self.wait(IDLE_INTERVAL_SECONDS)
                    continue
                self._run_claims(registry, claims)
        logger.info(
            "Crawler worker stopped",
            extra={"event": "crawler_worker_stopped", "component": "crawler-worker"},
        )


def main() -> None:
    configure_logging("classical-bot")
    try:
        config = CrawlerWorkerConfig.from_environment()
    except ValueError as error:
        raise SystemExit(str(error)) from error
    worker = CrawlerWorker(config)
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    signal.signal(signal.SIGINT, lambda *_: worker.stop())
    worker.run()


if __name__ == "__main__":
    main()
