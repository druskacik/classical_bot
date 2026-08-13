import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import deployment.caprover_updater as caprover
from deployment import deferred_deployment as deferred


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


class DeferredDeploymentTests(unittest.TestCase):
    def config(self, root: Path, **overrides):
        values = {
            "repository": "https://github.com/example/repository.git",
            "deploy_webhook": "https://captain.test/hook",
            "state_path": root / "deployment-state.json",
            "retry_interval_seconds": 300,
            "drain_timeout_seconds": 3600,
        }
        values.update(overrides)
        return deferred.DeferredDeploymentConfig(**values)

    def test_internal_state_path_and_retry_interval_are_not_overridden(self):
        with patch.dict(
            os.environ,
            {
                "SCRAPER_DEPLOY_STATE_PATH": "/tmp/override.json",
                "SCRAPER_UPDATE_RETRY_SECONDS": "1",
            },
            clear=True,
        ):
            config = deferred.DeferredDeploymentConfig.from_environment()
        self.assertEqual(config.state_path, deferred.DEFAULT_STATE_PATH)
        self.assertEqual(config.retry_interval_seconds, deferred.UPDATE_RETRY_SECONDS)

    def test_newer_commit_starts_drain_without_requesting_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            coordinator = deferred.DeferredDeploymentCoordinator(config)
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(caprover, "remote_commit", return_value=NEW_SHA),
                patch.object(caprover, "request_deployment") as deploy,
            ):
                coordinator.check_for_update()

            self.assertTrue(coordinator.pending_event.is_set())
            self.assertEqual(coordinator.latest_commit, NEW_SHA)
            deploy.assert_not_called()

    def test_matching_commit_consumes_request_without_draining(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            coordinator = deferred.DeferredDeploymentCoordinator(config)
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(caprover, "remote_commit", return_value=OLD_SHA),
            ):
                coordinator.check_for_update()

            self.assertFalse(coordinator.pending_event.is_set())

    def test_failed_check_leaves_request_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            coordinator = deferred.DeferredDeploymentCoordinator(config)
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(caprover, "remote_commit", side_effect=RuntimeError("offline")),
            ):
                coordinator.check_for_update()

            self.assertFalse(coordinator.pending_event.is_set())

    def test_drain_deadline_stops_active_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = deferred.DeferredDeploymentCoordinator(
                self.config(Path(temporary), drain_timeout_seconds=60)
            )
            coordinator.pending_event.set()
            coordinator.pending_since = 100
            shutdown = threading.Event()
            worker_stop = threading.Event()

            def stop_monitor(_seconds):
                shutdown.set()
                return True

            with (
                patch.object(deferred, "monotonic", return_value=161),
                patch.object(shutdown, "wait", side_effect=stop_monitor),
            ):
                coordinator.monitor(shutdown, worker_stop)

            self.assertTrue(worker_stop.is_set())

    def test_drain_deadline_stops_both_analyzer_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            coordinator = deferred.DeferredDeploymentCoordinator(
                self.config(Path(temporary), drain_timeout_seconds=60)
            )
            coordinator.pending_event.set()
            coordinator.pending_since = 100
            shutdown = threading.Event()
            programme_stop = threading.Event()
            classifier_stop = threading.Event()

            def stop_monitor(_seconds):
                shutdown.set()
                return True

            with (
                patch.object(deferred, "monotonic", return_value=161),
                patch.object(shutdown, "wait", side_effect=stop_monitor),
            ):
                coordinator.monitor(shutdown, [programme_stop, classifier_stop])

            self.assertTrue(programme_stop.is_set())
            self.assertTrue(classifier_stop.is_set())


if __name__ == "__main__":
    unittest.main()
