from __future__ import annotations

import json
import logging
import os
import signal
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Callable

from analyzers.programme_supervisor import (
    ProgrammeAnalysisSupervisor,
    ProgrammeSupervisorConfig,
)
from automation.codex_auth import CodexAuthPause
from automation.notifications import Notification, send_notification
from observability import configure_logging


logger = logging.getLogger(__name__)
IDLE_INTERVAL_SECONDS = 300
FAILURE_BACKOFF_SECONDS = 900


def positive_integer(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < 1:
        raise ValueError(f"{name} must be at least 1")
    return parsed


def log(message: str) -> None:
    event = "programme_worker_status"
    if message.startswith("Starting programme analysis batch"):
        event = "programme_worker_batch_started"
    elif message.startswith("Programme analysis batch finished"):
        event = "programme_worker_batch_completed"
    elif message.startswith("Programme analysis queue is drained"):
        event = "programme_worker_queue_idle"
    elif message.startswith("Programme analysis batch failed"):
        event = "programme_worker_batch_failed"
    elif message.startswith("Received signal"):
        event = "programme_worker_signal_received"
    elif message == "Stopped":
        event = "programme_worker_stopped"
    level = logging.ERROR if event.endswith("_failed") else logging.INFO
    logger.log(level, message, extra={"event": event, "component": "programme-analyzer"})


@dataclass(frozen=True)
class WorkerConfig:
    batch_size: int
    concurrency: int
    idle_interval_seconds: int
    failure_backoff_seconds: int
    batch_timeout_seconds: int
    stall_timeout_seconds: int

    @classmethod
    def from_environment(cls) -> WorkerConfig:
        return cls(
            batch_size=positive_integer(
                os.getenv("CONCERT_PROGRAM_BATCH_SIZE", "100"),
                "CONCERT_PROGRAM_BATCH_SIZE",
            ),
            concurrency=positive_integer(
                os.getenv("CONCERT_PROGRAM_CONCURRENCY", "4"),
                "CONCERT_PROGRAM_CONCURRENCY",
            ),
            idle_interval_seconds=IDLE_INTERVAL_SECONDS,
            failure_backoff_seconds=FAILURE_BACKOFF_SECONDS,
            batch_timeout_seconds=positive_integer(
                os.getenv("CONCERT_PROGRAM_BATCH_TIMEOUT_SECONDS", "72000"),
                "CONCERT_PROGRAM_BATCH_TIMEOUT_SECONDS",
            ),
            stall_timeout_seconds=positive_integer(
                os.getenv("CONCERT_PROGRAM_STALL_TIMEOUT_SECONDS", "2400"),
                "CONCERT_PROGRAM_STALL_TIMEOUT_SECONDS",
            ),
        )


@dataclass(frozen=True)
class BatchOutcome:
    return_code: int
    status: str | None
    selected_count: int | None
    group_count: int | None
    completed_count: int | None
    failure_count: int | None
    auth_reason_code: str | None = None


def load_batch_result(path: Path, return_code: int) -> BatchOutcome:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        status = str(payload["status"])
        if status not in {"empty", "completed", "fatal", "auth_required"}:
            raise ValueError(f"unexpected batch status {status!r}")

        def optional_integer(name: str) -> int | None:
            value = payload.get(name)
            if value is None:
                return None
            parsed = int(value)
            if parsed < 0:
                raise ValueError(f"{name} must not be negative")
            return parsed

        return BatchOutcome(
            return_code=return_code,
            status=status,
            selected_count=optional_integer("selected_count"),
            group_count=optional_integer("group_count"),
            completed_count=optional_integer("completed_count"),
            failure_count=optional_integer("failure_count"),
            auth_reason_code=(
                str(payload["auth_reason_code"])
                if payload.get("auth_reason_code")
                else None
            ),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return BatchOutcome(return_code, None, None, None, None, None)


class ProgrammeAnalyzerWorker:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        stop_event: threading.Event | None = None,
        drain_event: threading.Event | None = None,
        before_batch: Callable[[], None] | None = None,
        result_path: Path | None = None,
        auth_pause_path: Path | None = None,
    ) -> None:
        self.config = config
        self.stop_event = stop_event or threading.Event()
        self.drain_event = drain_event or threading.Event()
        self.before_batch = before_batch
        self.supervisor = ProgrammeAnalysisSupervisor(
            ProgrammeSupervisorConfig(
                batch_timeout_seconds=config.batch_timeout_seconds,
                stall_timeout_seconds=config.stall_timeout_seconds,
            )
        )
        self.result_path = result_path or Path(tempfile.gettempdir()) / (
            f"classical-programme-analysis-{os.getpid()}.result.json"
        )
        self.auth_pause = CodexAuthPause.for_service("classical-bot", auth_pause_path)

    def stop(self, signum: int | None = None, _frame: object = None) -> None:
        if signum is not None:
            log(f"Received signal {signum}; stopping")
        self.stop_event.set()

    def run_batch(self) -> BatchOutcome:
        self.result_path.unlink(missing_ok=True)
        command = [
            sys.executable,
            "-m",
            "analyzers.analyze_concert_programs",
            "--commit",
            "--limit",
            str(self.config.batch_size),
            "--concurrency",
            str(self.config.concurrency),
            "--heartbeat-path",
            str(self.supervisor.heartbeat_path),
            "--result-path",
            str(self.result_path),
        ]
        log(
            "Starting programme analysis batch "
            f"(limit: {self.config.batch_size}, concurrency: {self.config.concurrency})"
        )
        return_code = self.supervisor.run(command, self.stop_event)
        outcome = load_batch_result(self.result_path, return_code)
        self.result_path.unlink(missing_ok=True)
        log(
            "Programme analysis batch finished "
            f"(status: {outcome.status or 'unknown'}, selected: "
            f"{outcome.selected_count}, failures: {outcome.failure_count}, "
            f"return code: {return_code})"
        )
        return outcome

    def wait(self, seconds: int) -> None:
        deadline = monotonic() + seconds
        while not self.stop_event.is_set() and not self.drain_event.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            self.stop_event.wait(min(1, remaining))

    def wait_for_authentication(self) -> None:
        state = self.auth_pause.load() or {}
        logger.warning(
            "Programme analysis is paused until Codex is reauthenticated",
            extra={
                "event": "codex_auth_pause_active",
                "component": "programme-analyzer",
                "reason_code": state.get("reason_code", "unknown"),
            },
        )
        if self.auth_pause.auth_file_changed():
            resumed, failure = self.auth_pause.verify_and_resume(cwd=Path.cwd())
            if resumed:
                logger.info(
                    "Codex authentication verified; programme analysis resumed",
                    extra={
                        "event": "codex_auth_restored",
                        "component": "programme-analyzer",
                    },
                )
                send_notification(
                    Notification(
                        title="Codex authentication restored",
                        message="Programme analysis authentication was verified and processing resumed.",
                        severity="info",
                    )
                )
                return
            logger.warning(
                "Codex authentication recovery check did not succeed",
                extra={
                    "event": "codex_auth_recovery_failed",
                    "component": "programme-analyzer",
                    "reason_code": failure,
                },
            )
        self.wait(60)

    def run(self) -> None:
        log(
            "Running continuous programme analysis "
            f"in batches of {self.config.batch_size} with concurrency "
            f"{self.config.concurrency}"
        )
        try:
            while not self.stop_event.is_set() and not self.drain_event.is_set():
                if self.auth_pause.is_paused():
                    self.wait_for_authentication()
                    continue
                if self.before_batch is not None:
                    self.before_batch()
                if self.drain_event.is_set():
                    log("Deployment pending; programme analyzer is drained")
                    break
                outcome = self.run_batch()
                if self.stop_event.is_set() or self.drain_event.is_set():
                    break
                if outcome.status in {"empty", "completed"}:
                    if (outcome.selected_count or 0) < self.config.batch_size:
                        log(
                            "Programme analysis queue is drained; "
                            f"checking again in {self.config.idle_interval_seconds} seconds"
                        )
                        self.wait(self.config.idle_interval_seconds)
                    continue
                if outcome.status == "auth_required":
                    created = self.auth_pause.pause(
                        outcome.auth_reason_code or "login_required",
                        {"component": "programme-analyzer"},
                    )
                    if created:
                        logger.critical(
                            "Programme analysis entered persistent Codex authentication pause",
                            extra={
                                "event": "codex_auth_required",
                                "component": "programme-analyzer",
                                "reason_code": outcome.auth_reason_code or "login_required",
                            },
                        )
                        send_notification(
                            Notification(
                                title="Codex authentication required",
                                message=(
                                    "Programme analysis stopped without consuming concert attempts. "
                                    "Reauthenticate the classical-bot Codex credential directory. "
                                    f"Reason: {outcome.auth_reason_code or 'login_required'}."
                                ),
                                severity="critical",
                            )
                        )
                    continue
                log(
                    "Programme analysis batch failed; retrying after "
                    f"{self.config.failure_backoff_seconds} seconds"
                )
                self.wait(self.config.failure_backoff_seconds)
        finally:
            self.supervisor.stop()
            self.result_path.unlink(missing_ok=True)
            log("Stopped")


def main() -> None:
    configure_logging("classical-bot")
    try:
        config = WorkerConfig.from_environment()
    except ValueError as error:
        raise SystemExit(str(error)) from error
    worker = ProgrammeAnalyzerWorker(config)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run()


if __name__ == "__main__":
    main()
