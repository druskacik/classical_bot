import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation import run_potential_event_classifier_worker as worker_module


class PotentialEventClassifierWorkerTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "idle_interval_seconds": 300,
            "failure_backoff_seconds": 900,
            "turn_timeout_seconds": 1800,
        }
        values.update(overrides)
        return worker_module.ClassifierWorkerConfig(**values)

    def test_internal_wait_policy_is_not_overridden_by_environment(self):
        with patch.dict(
            os.environ,
            {
                "POTENTIAL_EVENT_CLASSIFIER_IDLE_SECONDS": "1",
                "POTENTIAL_EVENT_CLASSIFIER_FAILURE_BACKOFF_SECONDS": "1",
            },
            clear=True,
        ):
            config = worker_module.ClassifierWorkerConfig.from_environment()
        self.assertEqual(config.idle_interval_seconds, 300)
        self.assertEqual(config.failure_backoff_seconds, 900)

    def test_process_guards_are_derived_from_turn_timeout(self):
        worker = worker_module.PotentialEventClassifierWorker(self.config())

        self.assertEqual(worker.supervisor.config.batch_timeout_seconds, 86400)
        self.assertEqual(worker.supervisor.config.stall_timeout_seconds, 2400)
        self.assertIn(
            "potential-event-classification-",
            worker.supervisor.heartbeat_path.name,
        )

    def test_worker_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(worker_module.enabled_from_environment())
        with patch.dict(os.environ, {"POTENTIAL_EVENT_CLASSIFIER_ENABLED": "true"}):
            self.assertTrue(worker_module.enabled_from_environment())

    def test_promotion_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(worker_module.promotion_enabled_from_environment())
        with patch.dict(
            os.environ,
            {"POTENTIAL_EVENT_CLASSIFIER_PROMOTION_ENABLED": "true"},
        ):
            self.assertTrue(worker_module.promotion_enabled_from_environment())

    def test_source_command_uses_only_one_explicit_turn_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.json"
            worker = worker_module.PotentialEventClassifierWorker(
                self.config(), result_path=result_path
            )

            def run_child(command, _stop_event):
                result_path.write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "selected_count": 40,
                            "source": "Example Hall",
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(command[command.index("--timeout-seconds") + 1], "1800")
                self.assertNotIn("--include-past", command)
                return 0

            with patch.object(worker.supervisor, "run", side_effect=run_child):
                outcome = worker.run_source()
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.source, "Example Hall")

    def test_source_command_promotes_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.json"
            worker = worker_module.PotentialEventClassifierWorker(
                self.config(promotion_enabled=True),
                result_path=result_path,
            )

            def run_child(command, _stop_event):
                self.assertIn("--promote", command)
                result_path.write_text(
                    json.dumps(
                        {
                            "status": "empty",
                            "selected_count": 0,
                            "source": None,
                        }
                    ),
                    encoding="utf-8",
                )
                return 0

            with patch.object(worker.supervisor, "run", side_effect=run_child):
                worker.run_source()

    def test_empty_queue_waits_before_polling_again(self):
        worker = worker_module.PotentialEventClassifierWorker(self.config())

        def wait_then_stop(seconds):
            self.assertEqual(seconds, 300)
            worker.stop_event.set()

        with (
            patch.object(
                worker,
                "run_source",
                return_value=worker_module.ClassifierOutcome(0, "empty", 0, None),
            ),
            patch.object(worker, "wait", side_effect=wait_then_stop),
            patch.object(worker.supervisor, "stop"),
        ):
            worker.run()

    def test_fatal_run_uses_failure_backoff(self):
        worker = worker_module.PotentialEventClassifierWorker(self.config())

        def wait_then_stop(seconds):
            self.assertEqual(seconds, 900)
            worker.stop_event.set()

        with (
            patch.object(
                worker,
                "run_source",
                return_value=worker_module.ClassifierOutcome(1, "fatal", 0, None),
            ),
            patch.object(worker, "wait", side_effect=wait_then_stop),
            patch.object(worker.supervisor, "stop"),
        ):
            worker.run()


if __name__ == "__main__":
    unittest.main()
