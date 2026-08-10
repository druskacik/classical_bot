import signal
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch

from automation import run_classical_bot_service as service_module


class ClassicalBotServiceTests(unittest.TestCase):
    def worker(self):
        worker = MagicMock()
        worker.stop_event = threading.Event()
        worker.stop.side_effect = worker.stop_event.set
        return worker

    def deployment(self):
        deployment = MagicMock()
        deployment.pending_event = threading.Event()
        deployment.config.retry_interval_seconds = 300
        return deployment

    def test_scheduler_starts_as_a_supervised_process_group(self):
        worker = self.worker()
        service = service_module.ClassicalBotService(worker, self.deployment())
        scheduler = MagicMock(pid=321)
        with (
            patch.object(service_module.subprocess, "Popen", return_value=scheduler) as popen,
            patch.object(service_module.threading, "Thread") as thread,
        ):
            service.start_scheduler()

        command = popen.call_args.args[0]
        self.assertEqual(command[-1], "--scheduler-only")
        popen.assert_called_once_with(command, start_new_session=True)
        thread.return_value.start.assert_called_once_with()

    def test_shutdown_signal_stops_worker_and_scheduler(self):
        worker = self.worker()
        service = service_module.ClassicalBotService(worker, self.deployment())
        service.scheduler = MagicMock(pid=321)
        service.scheduler.poll.return_value = None
        with patch.object(service_module.os, "killpg") as killpg:
            service.stop(signal.SIGTERM)

        self.assertTrue(worker.stop_event.is_set())
        killpg.assert_called_once_with(321, signal.SIGTERM)

    def test_shutdown_signal_stops_both_analyzer_workers(self):
        worker = self.worker()
        classifier = self.worker()
        service = service_module.ClassicalBotService(
            worker,
            self.deployment(),
            classifier,
        )
        service.scheduler = MagicMock(pid=321)
        service.scheduler.poll.return_value = None
        with patch.object(service_module.os, "killpg"):
            service.stop(signal.SIGTERM)

        self.assertTrue(worker.stop_event.is_set())
        self.assertTrue(classifier.stop_event.is_set())

    def test_unexpected_scheduler_exit_stops_analyzer(self):
        worker = self.worker()
        service = service_module.ClassicalBotService(worker, self.deployment())
        service.scheduler = MagicMock(pid=321)
        service.scheduler.wait.return_value = 2

        service._monitor_scheduler()

        self.assertTrue(service.failed)
        self.assertTrue(worker.stop_event.is_set())
        self.assertEqual(service.scheduler_exit_code, 2)

    def test_worker_crash_stops_scheduler_and_returns_failure(self):
        worker = self.worker()
        worker.run.side_effect = RuntimeError("broken")
        service = service_module.ClassicalBotService(worker, self.deployment())
        scheduler = MagicMock(pid=321)
        scheduler.poll.return_value = None
        scheduler.wait.return_value = -signal.SIGTERM

        def start_scheduler():
            service.scheduler = scheduler

        with (
            patch.object(service_module.signal, "signal"),
            patch.object(service, "start_scheduler", side_effect=start_scheduler),
            patch.object(service_module.os, "killpg") as killpg,
        ):
            return_code = service.run()

        self.assertEqual(return_code, 1)
        killpg.assert_called_once_with(321, signal.SIGTERM)

    def test_scheduler_is_force_killed_after_shutdown_grace(self):
        worker = self.worker()
        service = service_module.ClassicalBotService(worker, self.deployment())
        scheduler = MagicMock(pid=321)
        scheduler.poll.return_value = None
        scheduler.wait.side_effect = [
            subprocess.TimeoutExpired("scheduler", 45),
            -signal.SIGKILL,
        ]
        service.scheduler = scheduler
        with patch.object(service_module.os, "killpg") as killpg:
            service._finish_scheduler()

        self.assertEqual(
            [call.args for call in killpg.call_args_list],
            [(321, signal.SIGTERM), (321, signal.SIGKILL)],
        )

    def test_deployment_is_requested_only_after_worker_has_drained(self):
        order = []
        worker = self.worker()
        worker.run.side_effect = lambda: order.append("worker-drained")
        deployment = self.deployment()
        deployment.pending_event.set()
        service = service_module.ClassicalBotService(worker, deployment)
        scheduler = MagicMock(pid=321)
        scheduler.poll.return_value = 0

        def start_scheduler():
            service.scheduler = scheduler

        def request_deployment():
            order.append("deployment-requested")
            service.shutdown_event.set()
            return True

        deployment.request_deployment.side_effect = request_deployment
        with (
            patch.object(service_module.signal, "signal"),
            patch.object(service, "start_scheduler", side_effect=start_scheduler),
        ):
            self.assertEqual(service.run(), 0)

        self.assertEqual(order, ["worker-drained", "deployment-requested"])

    def test_deployment_waits_for_both_analyzer_workers(self):
        order = []
        worker = self.worker()
        classifier = self.worker()
        programme_can_drain = threading.Event()

        def run_programme():
            programme_can_drain.wait(timeout=1)
            order.append("programme-drained")

        def run_classifier():
            order.append("classifier-drained")
            programme_can_drain.set()

        worker.run.side_effect = run_programme
        classifier.run.side_effect = run_classifier
        deployment = self.deployment()
        deployment.pending_event.set()
        service = service_module.ClassicalBotService(worker, deployment, classifier)
        scheduler = MagicMock(pid=321)
        scheduler.poll.return_value = 0

        def start_scheduler():
            service.scheduler = scheduler

        def request_deployment():
            order.append("deployment-requested")
            service.shutdown_event.set()
            return True

        deployment.request_deployment.side_effect = request_deployment
        with (
            patch.object(service_module.signal, "signal"),
            patch.object(service, "start_scheduler", side_effect=start_scheduler),
        ):
            self.assertEqual(service.run(), 0)

        self.assertEqual(
            order,
            ["classifier-drained", "programme-drained", "deployment-requested"],
        )


if __name__ == "__main__":
    unittest.main()
