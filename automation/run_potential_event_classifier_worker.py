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


def enabled_from_environment() -> bool:
    return os.getenv("POTENTIAL_EVENT_CLASSIFIER_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def promotion_enabled_from_environment() -> bool:
    return os.getenv(
        "POTENTIAL_EVENT_CLASSIFIER_PROMOTION_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def log(message: str) -> None:
    event = "potential_event_classifier_worker_status"
    if message.startswith("Starting potential-event source"):
        event = "potential_event_classifier_worker_started"
    elif message.startswith("Potential-event source finished"):
        event = "potential_event_classifier_worker_completed"
    elif message.startswith("Potential-event classification queue is drained"):
        event = "potential_event_classifier_worker_idle"
    elif message.startswith("Potential-event classification failed"):
        event = "potential_event_classifier_worker_failed"
    level = logging.ERROR if event.endswith("_failed") else logging.INFO
    logger.log(level, message, extra={"event": event, "component": "potential-event-classifier"})


@dataclass(frozen=True)
class ClassifierWorkerConfig:
    idle_interval_seconds: int
    failure_backoff_seconds: int
    turn_timeout_seconds: int
    promotion_enabled: bool = False

    @classmethod
    def from_environment(cls) -> "ClassifierWorkerConfig":
        return cls(
            idle_interval_seconds=IDLE_INTERVAL_SECONDS,
            failure_backoff_seconds=FAILURE_BACKOFF_SECONDS,
            turn_timeout_seconds=positive_integer(
                os.getenv("POTENTIAL_EVENT_CLASSIFIER_TURN_TIMEOUT_SECONDS", "1800"),
                "POTENTIAL_EVENT_CLASSIFIER_TURN_TIMEOUT_SECONDS",
            ),
            promotion_enabled=promotion_enabled_from_environment(),
        )


@dataclass(frozen=True)
class ClassifierOutcome:
    return_code: int
    status: str | None
    selected_count: int | None
    source: str | None
    auth_reason_code: str | None = None


def load_result(path: Path, return_code: int) -> ClassifierOutcome:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        status = str(payload["status"])
        if status not in {"empty", "completed", "partial", "fatal", "auth_required"}:
            raise ValueError(f"unexpected status {status!r}")
        count = payload.get("selected_count")
        return ClassifierOutcome(
            return_code=return_code,
            status=status,
            selected_count=int(count) if count is not None else None,
            source=str(payload["source"]) if payload.get("source") is not None else None,
            auth_reason_code=(
                str(payload["auth_reason_code"])
                if payload.get("auth_reason_code") else None
            ),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return ClassifierOutcome(return_code, None, None, None)


class PotentialEventClassifierWorker:
    def __init__(
        self,
        config: ClassifierWorkerConfig,
        *,
        stop_event: threading.Event | None = None,
        drain_event: threading.Event | None = None,
        before_run: Callable[[], None] | None = None,
        result_path: Path | None = None,
        auth_pause_path: Path | None = None,
    ) -> None:
        self.config = config
        self.stop_event = stop_event or threading.Event()
        self.drain_event = drain_event or threading.Event()
        self.before_run = before_run
        self.supervisor = ProgrammeAnalysisSupervisor(
            ProgrammeSupervisorConfig(
                # A source is a bounded snapshot split into pages. Keep one
                # operator-facing timeout and derive the two process guards.
                batch_timeout_seconds=config.turn_timeout_seconds * 48,
                stall_timeout_seconds=config.turn_timeout_seconds + 600,
            ),
            heartbeat_path=Path(tempfile.gettempdir())
            / f"potential-event-classification-{os.getpid()}.heartbeat",
        )
        self.result_path = result_path or Path(tempfile.gettempdir()) / (
            f"potential-event-classification-{os.getpid()}.result.json"
        )
        self.auth_pause = CodexAuthPause.for_service("classical-bot", auth_pause_path)

    def stop(self, signum: int | None = None, _frame: object = None) -> None:
        if signum is not None:
            log(f"Received signal {signum}; stopping")
        self.stop_event.set()

    def run_source(self) -> ClassifierOutcome:
        self.result_path.unlink(missing_ok=True)
        command = [
            sys.executable,
            "-m",
            "analyzers.analyze_potential_events",
            "--commit",
            "--timeout-seconds",
            str(self.config.turn_timeout_seconds),
            "--heartbeat-path",
            str(self.supervisor.heartbeat_path),
            "--result-path",
            str(self.result_path),
        ]
        if self.config.promotion_enabled:
            command.append("--promote")
        log("Starting potential-event source classification")
        return_code = self.supervisor.run(command, self.stop_event)
        outcome = load_result(self.result_path, return_code)
        self.result_path.unlink(missing_ok=True)
        log(
            "Potential-event source finished "
            f"(status: {outcome.status or 'unknown'}, source: {outcome.source}, "
            f"selected: {outcome.selected_count}, return code: {return_code})"
        )
        return outcome

    def wait(self, seconds: int) -> None:
        deadline = monotonic() + seconds
        while not self.stop_event.is_set() and not self.drain_event.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(1, remaining))

    def wait_for_authentication(self) -> None:
        state = self.auth_pause.load() or {}
        logger.warning(
            "Potential-event classification is paused until Codex is reauthenticated",
            extra={
                "event": "codex_auth_pause_active",
                "component": "potential-event-classifier",
                "reason_code": state.get("reason_code", "unknown"),
            },
        )
        if self.auth_pause.auth_file_changed():
            resumed, failure = self.auth_pause.verify_and_resume(cwd=Path.cwd())
            if resumed:
                send_notification(
                    Notification(
                        title="Codex authentication restored",
                        message="Potential-event classification authentication was verified and processing resumed.",
                        severity="info",
                    )
                )
                return
            logger.warning(
                "Codex authentication recovery check did not succeed",
                extra={
                    "event": "codex_auth_recovery_failed",
                    "component": "potential-event-classifier",
                    "reason_code": failure,
                },
            )
        self.wait(60)

    def run(self) -> None:
        log("Running continuous potential-event source classification")
        try:
            while not self.stop_event.is_set() and not self.drain_event.is_set():
                if self.auth_pause.is_paused():
                    self.wait_for_authentication()
                    continue
                if self.before_run is not None:
                    self.before_run()
                if self.drain_event.is_set():
                    break
                outcome = self.run_source()
                if self.stop_event.is_set() or self.drain_event.is_set():
                    break
                if outcome.status in {"completed", "partial"}:
                    continue
                if outcome.status == "empty":
                    log(
                        "Potential-event classification queue is drained; "
                        f"checking again in {self.config.idle_interval_seconds} seconds"
                    )
                    self.wait(self.config.idle_interval_seconds)
                    continue
                if outcome.status == "auth_required":
                    created = self.auth_pause.pause(
                        outcome.auth_reason_code or "login_required",
                        {"component": "potential-event-classifier"},
                    )
                    if created:
                        logger.critical(
                            "Potential-event classification entered persistent Codex authentication pause",
                            extra={
                                "event": "codex_auth_required",
                                "component": "potential-event-classifier",
                                "reason_code": outcome.auth_reason_code or "login_required",
                            },
                        )
                        send_notification(
                            Notification(
                                title="Codex authentication required",
                                message=(
                                    "Potential-event classification stopped without consuming "
                                    "event attempts. Reauthenticate the classical-bot Codex credential directory."
                                ),
                                severity="critical",
                            )
                        )
                    continue
                log(
                    "Potential-event classification failed; retrying after "
                    f"{self.config.failure_backoff_seconds} seconds"
                )
                self.wait(self.config.failure_backoff_seconds)
        finally:
            self.supervisor.stop()
            self.result_path.unlink(missing_ok=True)


def main() -> None:
    configure_logging("classical-bot")
    try:
        config = ClassifierWorkerConfig.from_environment()
    except ValueError as error:
        raise SystemExit(str(error)) from error
    worker = PotentialEventClassifierWorker(config)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run()


if __name__ == "__main__":
    main()
