import signal
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

    def test_shutdown_stops_all_workers(self):
        programme = self.worker()
        classifier = self.worker()
        crawler = self.worker()
        service = service_module.ClassicalBotService(
            programme, self.deployment(), classifier, crawler
        )

        service.stop(signal.SIGTERM)

        self.assertTrue(programme.stop_event.is_set())
        self.assertTrue(classifier.stop_event.is_set())
        self.assertTrue(crawler.stop_event.is_set())

    def test_crawler_failure_stops_service(self):
        programme = self.worker()
        crawler = self.worker()
        crawler.run.side_effect = RuntimeError("broken")
        service = service_module.ClassicalBotService(
            programme, self.deployment(), crawler_worker=crawler
        )

        service._run_crawler()

        self.assertTrue(service.failed)
        self.assertTrue(service.shutdown_event.is_set())
        self.assertTrue(programme.stop_event.is_set())

    def test_deployment_waits_for_all_workers(self):
        order = []
        programme = self.worker()
        classifier = self.worker()
        crawler = self.worker()
        deployment = self.deployment()
        deployment.pending_event.set()
        programme.run.side_effect = lambda: order.append("programme-drained")
        classifier.run.side_effect = lambda: order.append("classifier-drained")
        crawler.run.side_effect = lambda: order.append("crawler-drained")
        service = service_module.ClassicalBotService(
            programme, deployment, classifier, crawler
        )

        def request_deployment():
            order.append("deployment-requested")
            service.shutdown_event.set()
            return True

        deployment.request_deployment.side_effect = request_deployment
        with patch.object(service_module.signal, "signal"):
            self.assertEqual(service.run(), 0)

        self.assertEqual(order[-1], "deployment-requested")
        self.assertCountEqual(
            order[:-1], ["programme-drained", "classifier-drained", "crawler-drained"]
        )


if __name__ == "__main__":
    unittest.main()
