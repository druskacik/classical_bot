from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Iterable

from deployment.caprover_updater import CapRoverUpdater, CapRoverUpdaterConfig
DEFAULT_REPOSITORY = "https://github.com/druskacik/classical_bot.git"
DEFAULT_STATE_PATH = Path("/var/lib/classical-bot/deployment-state.json")
UPDATE_RETRY_SECONDS = 300
logger = logging.getLogger(__name__)


def positive_integer(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < 1:
        raise ValueError(f"{name} must be at least 1")
    return parsed


def log(message: str) -> None:
    event = "deferred_deployment_status"
    if message.startswith("Update found"):
        event = "deployment_drain_started"
    elif message.startswith("Deployment drain deadline"):
        event = "deployment_drain_deadline_reached"
    elif message.startswith("Could not"):
        event = "deployment_check_failed"
    elif message.startswith("Automatic updates disabled"):
        event = "deployment_updates_disabled"
    elif message.startswith("Requested CapRover deployment"):
        event = "deployment_requested"
    level = logging.WARNING if event.endswith(("_failed", "_disabled", "_reached")) else logging.INFO
    logger.log(level, message, extra={"event": event, "component": "deployment-coordinator"})


@dataclass(frozen=True)
class DeferredDeploymentConfig:
    repository: str
    deploy_webhook: str | None
    state_path: Path
    retry_interval_seconds: int
    drain_timeout_seconds: int

    @classmethod
    def from_environment(cls) -> DeferredDeploymentConfig:
        return cls(
            repository=os.getenv("SCRAPER_REPOSITORY", DEFAULT_REPOSITORY),
            deploy_webhook=os.getenv("SCRAPER_DEPLOY_WEBHOOK") or None,
            state_path=DEFAULT_STATE_PATH,
            retry_interval_seconds=UPDATE_RETRY_SECONDS,
            drain_timeout_seconds=positive_integer(
                os.getenv("CONCERT_PROGRAM_DEPLOY_DRAIN_TIMEOUT_SECONDS", "3600"),
                "CONCERT_PROGRAM_DEPLOY_DRAIN_TIMEOUT_SECONDS",
            ),
        )


class DeferredDeploymentCoordinator:
    def __init__(self, config: DeferredDeploymentConfig) -> None:
        self.config = config
        self.pending_event = threading.Event()
        self.latest_commit: str | None = None
        self.pending_since: float | None = None
        self._check_lock = threading.Lock()
        self.updater = CapRoverUpdater(
            CapRoverUpdaterConfig(
                repository=config.repository,
                deploy_webhook=config.deploy_webhook,
                webhook_environment_name="SCRAPER_DEPLOY_WEBHOOK",
                state_path=config.state_path,
            ),
            log,
        )

    def check_for_update(self) -> None:
        if self.pending_event.is_set():
            return
        if not self._check_lock.acquire(blocking=False):
            return
        try:
            update = self.updater.find_update()
            if update is not None:
                self.latest_commit = update
                self.pending_since = monotonic()
                self.pending_event.set()
                log(
                    "Update found; draining Codex analysis before deploying "
                    f"{update[:12]}"
                )
        finally:
            self._check_lock.release()

    def monitor(
        self,
        shutdown_event: threading.Event,
        worker_stop_events: threading.Event | Iterable[threading.Event],
    ) -> None:
        stop_events = (
            [worker_stop_events]
            if isinstance(worker_stop_events, threading.Event)
            else list(worker_stop_events)
        )
        next_check_at = 0.0
        deadline_logged = False
        while not shutdown_event.is_set():
            now = monotonic()
            if not self.pending_event.is_set() and now >= next_check_at:
                self.check_for_update()
                next_check_at = monotonic() + self.config.retry_interval_seconds
            if (
                self.pending_event.is_set()
                and self.pending_since is not None
                and monotonic() - self.pending_since >= self.config.drain_timeout_seconds
                and any(not event.is_set() for event in stop_events)
            ):
                if not deadline_logged:
                    log(
                        "Deployment drain deadline reached; interrupting the active "
                        "Codex analysis batches"
                    )
                    deadline_logged = True
                for event in stop_events:
                    event.set()
            shutdown_event.wait(1)

    def request_deployment(self) -> bool:
        if self.latest_commit is None:
            return False
        return self.updater.request_update(self.latest_commit)
