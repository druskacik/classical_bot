import os
import signal
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from automation import run_crawler_worker as worker_module


class CrawlerWorkerTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "concurrency": 5,
            "timeout_seconds": 1800,
            "terminate_grace_seconds": 30,
            "lease_seconds": 2100,
            "history_retention_days": 90,
        }
        values.update(overrides)
        return worker_module.CrawlerWorkerConfig(**values)

    def test_environment_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = worker_module.CrawlerWorkerConfig.from_environment()
        self.assertEqual(config.concurrency, 5)
        self.assertEqual(config.timeout_seconds, 1800)
        self.assertEqual(config.history_retention_days, 90)

    def test_lease_must_cover_timeout_and_termination_grace(self):
        with patch.dict(
            os.environ,
            {
                "CRAWLER_TIMEOUT_SECONDS": "100",
                "CRAWLER_TERMINATE_GRACE_SECONDS": "30",
                "CRAWLER_LEASE_SECONDS": "120",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must cover"):
                worker_module.CrawlerWorkerConfig.from_environment()

    def test_discovers_repository_relative_paths(self):
        root = Path("/repo")
        files = [root / "crawlers/sk/example/main.py", root / "crawlers/cz/other/main.py"]
        with patch.object(Path, "glob", return_value=files):
            self.assertEqual(
                worker_module.discover_crawler_paths(root),
                ["crawlers/cz/other", "crawlers/sk/example"],
            )

    def test_launch_uses_module_and_process_group(self):
        worker = worker_module.CrawlerWorker(self.config())
        process = MagicMock(pid=123)
        with patch.object(worker_module.subprocess, "Popen", return_value=process) as popen:
            worker._launch({"id": 7, "crawler_path": "crawlers/sk/example"})
        popen.assert_called_once_with(
            [worker_module.sys.executable, "-m", "crawlers.sk.example.main"],
            cwd=worker.repository_root,
            start_new_session=True,
        )

    def test_failed_process_is_recorded(self):
        registry = MagicMock()
        process = MagicMock(pid=123)
        process.poll.return_value = 2
        worker = worker_module.CrawlerWorker(self.config())
        with patch.object(worker, "_launch") as launch:
            launch.return_value = worker_module.RunningCrawler(7, "crawlers/sk/example", process, 0)
            with patch.object(worker_module, "monotonic", return_value=1):
                worker._run_claims(registry, [{"id": 7, "crawler_path": "crawlers/sk/example"}])
        registry.finish.assert_called_once_with(7, "failed", 2)

    def test_timeout_terminates_then_records_timeout(self):
        registry = MagicMock()
        process = MagicMock(pid=123)
        process.poll.side_effect = [None, None, -signal.SIGTERM]
        worker = worker_module.CrawlerWorker(self.config(timeout_seconds=1))
        with (
            patch.object(worker, "_launch") as launch,
            patch.object(worker_module, "monotonic", side_effect=[0, 2, 3]),
            patch.object(worker_module.os, "killpg") as killpg,
        ):
            launch.return_value = worker_module.RunningCrawler(7, "crawlers/sk/example", process, 0)
            worker._run_claims(registry, [{"id": 7, "crawler_path": "crawlers/sk/example"}])
        killpg.assert_called_once_with(123, signal.SIGTERM)
        registry.finish.assert_called_once_with(7, "timed_out", -signal.SIGTERM)

    def test_empty_eligible_queue_waits_and_uses_fixed_daily_interval(self):
        registry = MagicMock()
        registry.__enter__.return_value = registry
        registry.claim.return_value = []
        worker = worker_module.CrawlerWorker(
            self.config(), registry_factory=lambda: registry
        )

        def stop_after_wait(seconds):
            self.assertEqual(seconds, worker_module.IDLE_INTERVAL_SECONDS)
            worker.stop_event.set()

        with (
            patch.object(
                worker_module,
                "discover_crawler_paths",
                return_value=["crawlers/sk/example"],
            ),
            patch.object(worker, "wait", side_effect=stop_after_wait),
        ):
            worker.run()

        registry.claim.assert_called_once_with(
            ["crawlers/sk/example"],
            limit=5,
            worker_id=worker.worker_id,
            lease_seconds=2100,
            minimum_interval_seconds=86400,
        )


if __name__ == "__main__":
    unittest.main()
