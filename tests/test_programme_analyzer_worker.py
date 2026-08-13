import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import automation.run_programme_analyzer_worker as worker_module


class ProgrammeAnalyzerWorkerTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "batch_size": 100,
            "concurrency": 4,
            "idle_interval_seconds": 300,
            "failure_backoff_seconds": 900,
            "batch_timeout_seconds": 72000,
            "stall_timeout_seconds": 2400,
        }
        values.update(overrides)
        return worker_module.WorkerConfig(**values)

    def test_environment_defaults(self):
        with patch.dict(
            os.environ,
            {
                "CONCERT_PROGRAM_IDLE_INTERVAL_SECONDS": "1",
                "CONCERT_PROGRAM_FAILURE_BACKOFF_SECONDS": "1",
            },
            clear=True,
        ):
            config = worker_module.WorkerConfig.from_environment()
        self.assertEqual(config.batch_size, 100)
        self.assertEqual(config.concurrency, 4)
        self.assertEqual(config.idle_interval_seconds, 300)
        self.assertEqual(config.failure_backoff_seconds, 900)

    def test_batch_command_and_result_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "last-batch-result.json"
            worker = worker_module.ProgrammeAnalyzerWorker(
                self.config(), result_path=result_path
            )

            def run_child(command, _stop_event):
                result_path.write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "selected_count": 100,
                            "group_count": 60,
                            "completed_count": 98,
                            "failure_count": 2,
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(command[command.index("--limit") + 1], "100")
                self.assertEqual(command[command.index("--concurrency") + 1], "4")
                self.assertIn("--heartbeat-path", command)
                self.assertIn("--result-path", command)
                return 1

            with patch.object(worker.supervisor, "run", side_effect=run_child):
                outcome = worker.run_batch()

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.selected_count, 100)
        self.assertEqual(outcome.failure_count, 2)

    def test_full_batch_repeats_immediately_then_drained_queue_waits(self):
        worker = worker_module.ProgrammeAnalyzerWorker(self.config())
        outcomes = [
            worker_module.BatchOutcome(0, "completed", 100, 60, 100, 0),
            worker_module.BatchOutcome(0, "completed", 20, 15, 20, 0),
        ]

        def wait_then_stop(seconds):
            self.assertEqual(seconds, 300)
            worker.stop_event.set()

        with (
            patch.object(worker, "run_batch", side_effect=outcomes) as run_batch,
            patch.object(worker, "wait", side_effect=wait_then_stop) as wait,
            patch.object(worker.supervisor, "stop"),
        ):
            worker.run()

        self.assertEqual(run_batch.call_count, 2)
        wait.assert_called_once_with(300)

    def test_fatal_batch_uses_failure_backoff(self):
        worker = worker_module.ProgrammeAnalyzerWorker(self.config())

        def wait_then_stop(seconds):
            self.assertEqual(seconds, 900)
            worker.stop_event.set()

        with (
            patch.object(
                worker,
                "run_batch",
                return_value=worker_module.BatchOutcome(1, "fatal", 0, 0, None, None),
            ),
            patch.object(worker, "wait", side_effect=wait_then_stop),
            patch.object(worker.supervisor, "stop"),
        ):
            worker.run()

    def test_auth_required_batch_enters_persistent_pause_without_retrying(self):
        with tempfile.TemporaryDirectory() as temporary:
            pause_path = Path(temporary) / "auth-required.json"
            worker = worker_module.ProgrammeAnalyzerWorker(
                self.config(), auth_pause_path=pause_path
            )

            def wait_then_stop(seconds):
                self.assertEqual(seconds, 60)
                worker.stop_event.set()

            with (
                patch.object(
                    worker,
                    "run_batch",
                    return_value=worker_module.BatchOutcome(
                        1, "auth_required", 10, 10, None, None,
                        "refresh_token_revoked",
                    ),
                ) as run_batch,
                patch.object(worker, "wait", side_effect=wait_then_stop),
                patch.object(worker.supervisor, "stop"),
            ):
                worker.run()

            self.assertEqual(run_batch.call_count, 1)
            self.assertTrue(pause_path.exists())

    def test_signal_stops_worker_loop(self):
        worker = worker_module.ProgrammeAnalyzerWorker(self.config())
        worker.stop(signal.SIGTERM)
        self.assertTrue(worker.stop_event.is_set())

    def test_pending_deployment_drains_before_starting_another_batch(self):
        drain_event = worker_module.threading.Event()
        worker = worker_module.ProgrammeAnalyzerWorker(
            self.config(),
            drain_event=drain_event,
            before_batch=drain_event.set,
        )
        with (
            patch.object(worker, "run_batch") as run_batch,
            patch.object(worker.supervisor, "stop"),
        ):
            worker.run()

        run_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
