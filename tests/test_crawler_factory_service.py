import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import automation.run_crawler_factory_service as service
import deployment.caprover_updater as caprover


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
HEAD_SHA = "c" * 40


class ServiceConfigTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = service.ServiceConfig.from_environment()

        self.assertEqual(config.schedule_time.strftime("%H:%M"), "06:00")
        self.assertEqual(config.mode, "continuous")
        self.assertEqual(config.timezone.key, "Europe/Prague")
        self.assertEqual(config.update_interval_seconds, 300)
        self.assertEqual(config.idle_interval_seconds, 300)
        self.assertEqual(config.failure_backoff_seconds, 900)
        self.assertEqual(config.pr_poll_interval_seconds, 60)
        self.assertEqual(config.max_urls, 5)
        self.assertEqual(config.timeout_minutes, 60)
        self.assertEqual(config.validation_timeout_minutes, 15)

    def test_invalid_schedule_is_rejected(self):
        with patch.dict(
            os.environ,
            {"CRAWLER_FACTORY_SCHEDULE_TIME": "25:00"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "HH:MM"):
                service.ServiceConfig.from_environment()

    def test_invalid_mode_is_rejected(self):
        with patch.dict(os.environ, {"CRAWLER_FACTORY_MODE": "sometimes"}, clear=True):
            with self.assertRaisesRegex(ValueError, "continuous.*scheduled"):
                service.ServiceConfig.from_environment()

    def test_invalid_timezone_is_rejected(self):
        with patch.dict(
            os.environ,
            {"CRAWLER_FACTORY_TIMEZONE": "Europe/Nowhere"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "known timezone"):
                service.ServiceConfig.from_environment()

    def test_nonpositive_numeric_setting_is_rejected(self):
        with patch.dict(
            os.environ,
            {"CRAWLER_FACTORY_MAX_URLS": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "at least 1"):
                service.ServiceConfig.from_environment()

    def test_nonpositive_validation_timeout_is_rejected(self):
        with patch.dict(
            os.environ,
            {"CRAWLER_FACTORY_VALIDATION_TIMEOUT_MINUTES": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "at least 1"):
                service.ServiceConfig.from_environment()


class ScheduleTests(unittest.TestCase):
    def test_batch_is_due_after_schedule(self):
        now = datetime(2026, 7, 26, 6, 1, tzinfo=ZoneInfo("Europe/Prague"))

        self.assertTrue(service.batch_is_due(now, service.parse_schedule("06:00"), {}))

    def test_batch_is_not_due_before_schedule(self):
        now = datetime(2026, 7, 26, 5, 59, tzinfo=ZoneInfo("Europe/Prague"))

        self.assertFalse(service.batch_is_due(now, service.parse_schedule("06:00"), {}))

    def test_batch_is_not_repeated_on_same_date(self):
        now = datetime(2026, 7, 26, 12, 0, tzinfo=ZoneInfo("Europe/Prague"))
        state = {"last_factory_attempt_date": "2026-07-26"}

        self.assertFalse(service.batch_is_due(now, service.parse_schedule("06:00"), state))

    def test_batch_uses_local_date_after_daylight_saving_change(self):
        now = datetime(2026, 10, 25, 6, 0, tzinfo=ZoneInfo("Europe/Prague"))
        state = {"last_factory_attempt_date": "2026-10-24"}

        self.assertTrue(service.batch_is_due(now, service.parse_schedule("06:00"), state))


class PullRequestRepositoryTests(unittest.TestCase):
    def test_https_repository_matches_pull_request(self):
        self.assertTrue(service.pull_request_belongs_to_repository(
            "https://github.com/example/repository/pull/1",
            "https://github.com/example/repository.git",
        ))

    def test_ssh_repository_matches_pull_request(self):
        self.assertTrue(service.pull_request_belongs_to_repository(
            "https://github.com/example/repository/pull/1",
            "git@github.com:example/repository.git",
        ))

    def test_other_repository_does_not_match(self):
        self.assertFalse(service.pull_request_belongs_to_repository(
            "https://github.com/other/repository/pull/1",
            "https://github.com/example/repository.git",
        ))


class FactoryServiceTests(unittest.TestCase):
    def config(
        self,
        root: Path,
        webhook: str | None = "https://captain.test/hook",
        mode: str = service.CONTINUOUS_MODE,
    ):
        return service.ServiceConfig(
            repository="https://github.com/example/repository.git",
            mode=mode,
            schedule_time=service.parse_schedule("06:00"),
            timezone=ZoneInfo("Europe/Prague"),
            update_interval_seconds=300,
            idle_interval_seconds=300,
            failure_backoff_seconds=900,
            pr_poll_interval_seconds=60,
            deploy_webhook=webhook,
            max_urls=7,
            timeout_minutes=45,
            validation_timeout_minutes=20,
            state_path=root / "service-state.json",
        )

    def test_factory_attempt_is_persisted_before_child_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = service.FactoryService(self.config(root, mode=service.SCHEDULED_MODE))
            process = Mock()
            process.poll.return_value = 0
            process.returncode = 0

            with patch.object(subprocess, "Popen", return_value=process) as popen:
                result = supervisor.run_factory(
                    datetime(2026, 7, 26, 6, 0, tzinfo=ZoneInfo("Europe/Prague"))
                )

            state = service.load_service_state(root / "service-state.json")

        self.assertEqual(result.return_code, 0)
        self.assertEqual(state["last_factory_attempt_date"], "2026-07-26")
        command = popen.call_args.args[0]
        self.assertIn("https://github.com/example/repository.git", command)
        self.assertEqual(command[command.index("--max-urls") + 1], "7")
        self.assertEqual(command[command.index("--timeout-minutes") + 1], "45")
        self.assertEqual(command[command.index("--validation-timeout-minutes") + 1], "20")

    def test_factory_outcome_includes_pull_request_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            process = Mock()
            process.poll.return_value = 0
            process.returncode = 0
            payload = {
                "claimed_count": 5,
                "status": "pr_open",
                "pull_request_url": "https://github.com/example/repository/pull/1",
                "base_commit_sha": OLD_SHA,
            }
            with (
                patch.object(subprocess, "Popen", return_value=process),
                patch.object(service, "load_state", return_value=payload),
            ):
                result = supervisor.run_factory(
                    datetime(2026, 7, 26, 6, 0, tzinfo=ZoneInfo("Europe/Prague"))
                )

        self.assertEqual(result.claimed_count, 5)
        self.assertEqual(result.status, "pr_open")
        self.assertEqual(
            result.pull_request_url,
            "https://github.com/example/repository/pull/1",
        )
        self.assertEqual(result.base_commit_sha, OLD_SHA)

    def test_matching_commit_does_not_request_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(service, "remote_commit", return_value=OLD_SHA),
                patch.object(caprover, "request_deployment") as deploy,
            ):
                self.assertFalse(supervisor.check_for_update())

        deploy.assert_not_called()

    def test_matching_commit_with_trailing_newline_does_not_request_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            with (
                patch.dict(
                    os.environ,
                    {"CAPROVER_GIT_COMMIT_SHA": f"{OLD_SHA}\n"},
                    clear=True,
                ),
                patch.object(service, "remote_commit", return_value=OLD_SHA),
                patch.object(caprover, "request_deployment") as deploy,
            ):
                self.assertFalse(supervisor.check_for_update())

        deploy.assert_not_called()

    def test_new_commit_requests_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(service, "remote_commit", return_value=NEW_SHA),
                patch.object(service, "changed_paths_between", return_value=["automation/service.py"]),
                patch.object(caprover, "request_deployment") as deploy,
            ):
                self.assertTrue(supervisor.check_for_update())

        deploy.assert_called_once_with("https://captain.test/hook")

    def test_successful_request_is_suppressed_after_service_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            supervisor = service.FactoryService(config)
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(service, "remote_commit", return_value=NEW_SHA),
                patch.object(service, "changed_paths_between", return_value=["automation/service.py"]),
                patch.object(caprover, "request_deployment") as deploy,
            ):
                self.assertTrue(supervisor.check_for_update())
                restarted_supervisor = service.FactoryService(config)
                self.assertFalse(restarted_supervisor.check_for_update())

            deploy.assert_called_once()
            state = service.load_service_state(root / "service-state.json")
            self.assertEqual(state["last_deploy_request_sha"], NEW_SHA)

    def test_failed_request_can_retry_on_next_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(service, "remote_commit", return_value=NEW_SHA),
                patch.object(service, "changed_paths_between", return_value=["automation/service.py"]),
                patch.object(
                    caprover,
                    "request_deployment",
                    side_effect=[RuntimeError("failed"), None],
                ) as deploy,
            ):
                self.assertFalse(supervisor.check_for_update())
                self.assertTrue(supervisor.check_for_update())

        self.assertEqual(deploy.call_count, 2)

    def test_invalid_deployed_commit_disables_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(
                self.config(Path(temporary), mode=service.SCHEDULED_MODE)
            )
            with (
                patch.dict(
                    os.environ,
                    {"CAPROVER_GIT_COMMIT_SHA": "not-a-commit"},
                    clear=True,
                ),
                patch.object(service, "remote_commit") as remote,
                patch.object(caprover, "request_deployment") as deploy,
            ):
                self.assertFalse(supervisor.check_for_update())

        remote.assert_not_called()
        deploy.assert_not_called()

    def test_update_is_skipped_while_factory_child_is_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.child = Mock()
            supervisor.child.poll.return_value = None
            with patch.object(service, "remote_commit") as remote:
                self.assertFalse(supervisor.check_for_update())

        remote.assert_not_called()

    def test_service_loop_calls_update_check_without_obsolete_timestamp_argument(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(
                self.config(Path(temporary), mode=service.SCHEDULED_MODE)
            )
            today = datetime.now(supervisor.config.timezone).date().isoformat()
            supervisor.state["last_factory_attempt_date"] = today

            def stop_after_check():
                supervisor.stop_event.set()
                return False

            with (
                patch.object(service.signal, "signal"),
                patch.object(service, "prepare_git_authentication"),
                patch.object(
                    supervisor,
                    "check_for_update",
                    side_effect=stop_after_check,
                ) as check,
            ):
                supervisor.run()

        check.assert_called_once_with()

    def test_stop_signal_is_forwarded_to_active_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.child = Mock()
            supervisor.child.poll.return_value = None

            supervisor.stop(service.signal.SIGTERM)

        self.assertTrue(supervisor.stop_event.is_set())
        supervisor.child.send_signal.assert_called_once_with(service.signal.SIGTERM)

    def test_missing_webhook_disables_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary), webhook=None))
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(service, "remote_commit") as remote,
            ):
                self.assertFalse(supervisor.check_for_update())

        remote.assert_not_called()

    def test_git_authentication_is_configured_when_token_is_available(self):
        with (
            patch.dict(os.environ, {"GH_TOKEN": "secret"}, clear=True),
            patch.object(subprocess, "run") as run,
        ):
            service.prepare_git_authentication()

        run.assert_called_once_with(
            ["gh", "auth", "setup-git"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_crawler_only_update_is_recorded_without_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = service.FactoryService(self.config(root))
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(service, "remote_commit", return_value=NEW_SHA),
                patch.object(
                    service,
                    "changed_paths_between",
                    return_value=["crawlers/cz/example/main.py", "crawlers/sk/old/main.py"],
                ),
                patch.object(caprover, "request_deployment") as deploy,
            ):
                self.assertFalse(supervisor.check_for_update())

            state = service.load_service_state(root / "service-state.json")

        self.assertEqual(state["last_factory_checked_sha"], NEW_SHA)
        self.assertFalse(supervisor.deployment_pending)
        deploy.assert_not_called()

    def test_previously_classified_crawler_update_needs_no_new_diff(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.state["last_factory_checked_sha"] = NEW_SHA
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(service, "remote_commit", return_value=NEW_SHA),
                patch.object(service, "changed_paths_between") as changed,
            ):
                self.assertFalse(supervisor.check_for_update())

        changed.assert_not_called()
        self.assertFalse(supervisor.deployment_pending)

    def test_mixed_update_drains_even_when_deploy_request_is_suppressed(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            with (
                patch.dict(os.environ, {"CAPROVER_GIT_COMMIT_SHA": OLD_SHA}, clear=True),
                patch.object(service, "remote_commit", return_value=NEW_SHA),
                patch.object(
                    service,
                    "changed_paths_between",
                    return_value=["crawlers/cz/example/main.py", "automation/service.py"],
                ),
                patch.object(supervisor.updater, "check_for_update", return_value=False),
            ):
                supervisor.updater.last_check_conclusive = True
                self.assertFalse(supervisor.check_for_update())

        self.assertTrue(supervisor.deployment_pending)

    def test_inconclusive_update_check_drains_without_starting_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))

            def stop_after_wait(_seconds):
                supervisor.stop_event.set()

            with (
                patch.object(service.signal, "signal"),
                patch.object(service, "prepare_git_authentication"),
                patch.object(supervisor, "check_for_update", return_value=False),
                patch.object(supervisor, "run_factory") as run_factory,
                patch.object(supervisor, "wait", side_effect=stop_after_wait) as wait,
            ):
                supervisor.update_check_conclusive = False
                supervisor.run()

        run_factory.assert_not_called()
        wait.assert_called_once_with(300)

    def test_open_pending_pull_request_blocks_batch_and_waits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = service.FactoryService(self.config(root))
            supervisor.remember_pending_pull_request(
                "https://github.com/example/repository/pull/1", NEW_SHA
            )

            def stop_after_wait(_seconds):
                supervisor.stop_event.set()

            response = Mock(
                stdout=(
                    '{"state":"OPEN","mergedAt":null,"mergeStateStatus":"BEHIND",'
                    '"statusCheckRollup":[],"baseRefName":"master",'
                    f'"baseRefOid":"{NEW_SHA}",'
                    '"headRefName":"crawler-factory/example",'
                    f'"headRefOid":"{HEAD_SHA}","isCrossRepository":false}}'
                )
            )
            with (
                patch.object(service.signal, "signal"),
                patch.object(service, "prepare_git_authentication"),
                patch.object(service.subprocess, "run", return_value=response) as run,
                patch.object(supervisor, "run_factory") as run_factory,
                patch.object(supervisor, "wait", side_effect=stop_after_wait) as wait,
            ):
                supervisor.run()

        run.assert_called_once()
        run_factory.assert_not_called()
        wait.assert_called_once_with(60)

    def test_successful_but_unmerged_pull_request_keeps_waiting(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.remember_pending_pull_request(
                "https://github.com/example/repository/pull/1", NEW_SHA
            )
            response = Mock(
                stdout=(
                    '{"state":"OPEN","mergedAt":null,"mergeStateStatus":"CLEAN",'
                    '"statusCheckRollup":[{"conclusion":"SUCCESS"}],'
                    '"baseRefName":"master",'
                    f'"baseRefOid":"{NEW_SHA}",'
                    '"headRefName":"crawler-factory/example",'
                    f'"headRefOid":"{HEAD_SHA}","isCrossRepository":false}}'
                )
            )
            with (
                patch.object(service.subprocess, "run", return_value=response),
            ):
                self.assertTrue(supervisor.pending_pull_request_is_open())

        self.assertIn("pending_factory_pr_url", supervisor.state)

    def test_ready_pull_request_is_merged_with_observed_head_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            pr_url = "https://github.com/example/repository/pull/1"
            supervisor.remember_pending_pull_request(pr_url, NEW_SHA)
            response = Mock(stdout=json.dumps({
                "state": "OPEN", "mergedAt": None, "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {
                        "name": "test", "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                    {
                        "name": "crawler-factory-validation", "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                ],
                "baseRefName": "master", "baseRefOid": NEW_SHA,
                "headRefName": "crawler-factory/2026-08-09-example",
                "headRefOid": HEAD_SHA, "isCrossRepository": False,
            }))
            with patch.object(
                service.subprocess,
                "run",
                side_effect=[response, Mock()],
            ) as run:
                self.assertTrue(supervisor.pending_pull_request_is_open())

        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "gh", "pr", "merge", pr_url, "--squash", "--delete-branch",
                "--match-head-commit", HEAD_SHA,
            ],
        )
        self.assertNotIn("pending_factory_pr_merge_retry_at", supervisor.state)

    def test_missing_required_check_does_not_merge_pull_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.remember_pending_pull_request(
                "https://github.com/example/repository/pull/1", NEW_SHA
            )
            response = Mock(stdout=json.dumps({
                "state": "OPEN", "mergedAt": None, "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {
                        "name": "test", "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                ],
                "baseRefName": "master", "baseRefOid": NEW_SHA,
                "headRefName": "crawler-factory/2026-08-09-example",
                "headRefOid": HEAD_SHA, "isCrossRepository": False,
            }))
            with patch.object(service.subprocess, "run", return_value=response) as run:
                self.assertTrue(supervisor.pending_pull_request_is_open())

        run.assert_called_once()

    def test_failed_merge_is_backed_off_and_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            pr_url = "https://github.com/example/repository/pull/1"
            supervisor.remember_pending_pull_request(pr_url, NEW_SHA)
            response = Mock(stdout=json.dumps({
                "state": "OPEN", "mergedAt": None, "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {
                        "name": "test", "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                    {
                        "name": "crawler-factory-validation", "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                ],
                "baseRefName": "master", "baseRefOid": NEW_SHA,
                "headRefName": "crawler-factory/2026-08-09-example",
                "headRefOid": HEAD_SHA, "isCrossRepository": False,
            }))
            failure = subprocess.CalledProcessError(
                1, ["gh"], stderr="temporary GitHub failure"
            )
            with patch.object(
                service.subprocess,
                "run",
                side_effect=[response, failure, response],
            ) as run:
                self.assertTrue(supervisor.pending_pull_request_is_open())
                self.assertTrue(supervisor.pending_pull_request_is_open())
            restarted = service.FactoryService(self.config(Path(temporary)))

        self.assertEqual(run.call_count, 3)
        self.assertEqual(supervisor.state["pending_factory_pr_merge_head_sha"], HEAD_SHA)
        self.assertEqual(supervisor.state["pending_factory_pr_merge_attempts"], 1)
        self.assertIn("pending_factory_pr_merge_retry_at", supervisor.state)
        self.assertEqual(restarted.state["pending_factory_pr_merge_head_sha"], HEAD_SHA)

    def test_changed_head_clears_merge_retry_before_rechecking(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.remember_pending_pull_request(
                "https://github.com/example/repository/pull/1", NEW_SHA
            )
            supervisor.state.update({
                "pending_factory_pr_merge_head_sha": "d" * 40,
                "pending_factory_pr_merge_attempts": 2,
                "pending_factory_pr_merge_retry_at": "2099-01-01T00:00:00+00:00",
            })
            response = Mock(stdout=json.dumps({
                "state": "OPEN", "mergedAt": None, "mergeable": "MERGEABLE",
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [
                    {"name": "test", "status": "IN_PROGRESS", "conclusion": ""},
                ],
                "baseRefName": "master", "baseRefOid": NEW_SHA,
                "headRefName": "crawler-factory/2026-08-09-example",
                "headRefOid": HEAD_SHA, "isCrossRepository": False,
            }))
            with patch.object(service.subprocess, "run", return_value=response):
                self.assertTrue(supervisor.pending_pull_request_is_open())

        self.assertNotIn("pending_factory_pr_merge_head_sha", supervisor.state)
        self.assertNotIn("pending_factory_pr_merge_retry_at", supervisor.state)

    def test_pending_pull_request_is_updated_when_master_advances(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            pr_url = "https://github.com/example/repository/pull/1"
            supervisor.remember_pending_pull_request(pr_url, OLD_SHA)
            response = Mock(stdout=json.dumps({
                "state": "OPEN", "mergedAt": None, "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [
                    {
                        "name": "test", "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                    {
                        "name": "crawler-factory-validation", "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    },
                ], "baseRefName": "master",
                "baseRefOid": NEW_SHA,
                "headRefName": "crawler-factory/2026-08-07-example",
                "headRefOid": HEAD_SHA, "isCrossRepository": False,
            }))
            with (
                patch.object(
                    service.subprocess, "run", side_effect=[response, Mock()]
                ) as run,
            ):
                self.assertTrue(supervisor.pending_pull_request_is_open())

        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["gh", "pr", "update-branch", pr_url],
        )
        self.assertEqual(supervisor.state["pending_factory_pr_base_sha"], NEW_SHA)
        self.assertNotIn("pending_factory_pr_update_retry_at", supervisor.state)

    def test_current_pending_pull_request_is_not_updated(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.remember_pending_pull_request(
                "https://github.com/example/repository/pull/1", NEW_SHA
            )
            response = Mock(stdout=json.dumps({
                "state": "OPEN", "mergedAt": None, "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                "baseRefName": "master", "baseRefOid": NEW_SHA,
                "headRefName": "crawler-factory/2026-08-07-example",
                "headRefOid": HEAD_SHA, "isCrossRepository": False,
            }))
            with (
                patch.object(service.subprocess, "run", return_value=response) as run,
            ):
                self.assertTrue(supervisor.pending_pull_request_is_open())

        run.assert_called_once()

    def test_failed_pending_pull_request_update_is_backed_off_and_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            pr_url = "https://github.com/example/repository/pull/1"
            supervisor.remember_pending_pull_request(pr_url, OLD_SHA)
            response = Mock(stdout=json.dumps({
                "state": "OPEN", "mergedAt": None, "mergeStateStatus": "DIRTY",
                "statusCheckRollup": [], "baseRefName": "master",
                "baseRefOid": NEW_SHA,
                "headRefName": "crawler-factory/2026-08-07-example",
                "headRefOid": HEAD_SHA, "isCrossRepository": False,
            }))
            with (
                patch.object(
                    service.subprocess,
                    "run",
                    side_effect=[
                        response,
                        subprocess.CalledProcessError(1, "gh"),
                        response,
                    ],
                ) as run,
            ):
                self.assertTrue(supervisor.pending_pull_request_is_open())
                self.assertTrue(supervisor.pending_pull_request_is_open())

        self.assertEqual(run.call_count, 3)
        self.assertEqual(supervisor.state["pending_factory_pr_base_sha"], OLD_SHA)
        self.assertEqual(supervisor.state["pending_factory_pr_update_attempts"], 1)
        self.assertIn("pending_factory_pr_update_retry_at", supervisor.state)

    def test_unexpected_pending_pull_request_is_never_updated(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.remember_pending_pull_request(
                "https://github.com/example/repository/pull/1", OLD_SHA
            )
            response = Mock(stdout=json.dumps({
                "state": "OPEN", "mergedAt": None, "mergeStateStatus": "BEHIND",
                "statusCheckRollup": [], "baseRefName": "master",
                "baseRefOid": NEW_SHA, "headRefName": "somebody-elses-branch",
                "headRefOid": HEAD_SHA, "isCrossRepository": False,
            }))
            with patch.object(service.subprocess, "run", return_value=response) as run:
                self.assertTrue(supervisor.pending_pull_request_is_open())

        run.assert_called_once()
        self.assertEqual(supervisor.state["pending_factory_pr_base_sha"], OLD_SHA)

    def test_failed_pull_request_keeps_waiting(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.remember_pending_pull_request(
                "https://github.com/example/repository/pull/1", NEW_SHA
            )
            response = Mock(
                stdout=(
                    '{"state":"OPEN","mergedAt":null,"mergeStateStatus":"BLOCKED",'
                    '"statusCheckRollup":[{"conclusion":"FAILURE"}],'
                    '"baseRefName":"master",'
                    f'"baseRefOid":"{NEW_SHA}",'
                    '"headRefName":"crawler-factory/example",'
                    f'"headRefOid":"{HEAD_SHA}","isCrossRepository":false}}'
                )
            )
            with (
                patch.object(service.subprocess, "run", return_value=response),
            ):
                self.assertTrue(supervisor.pending_pull_request_is_open())

        self.assertIn("pending_factory_pr_url", supervisor.state)

    def test_merged_pull_request_clears_persisted_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            supervisor = service.FactoryService(config)
            supervisor.remember_pending_pull_request("https://github.com/example/repository/pull/1")
            response = Mock(
                stdout=(
                    '{"state":"MERGED","mergedAt":"2026-08-07T12:00:00Z",'
                    '"mergeStateStatus":"UNKNOWN","statusCheckRollup":[]}'
                )
            )
            with patch.object(service.subprocess, "run", return_value=response):
                self.assertFalse(supervisor.pending_pull_request_is_open())
            restarted = service.FactoryService(config)

        self.assertNotIn("pending_factory_pr_url", restarted.state)
        self.assertNotIn("pending_factory_pr_base_sha", restarted.state)

    def test_closed_unmerged_pull_request_clears_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.remember_pending_pull_request("https://github.com/example/repository/pull/1")
            response = Mock(
                stdout=(
                    '{"state":"CLOSED","mergedAt":null,"mergeStateStatus":"UNKNOWN",'
                    '"statusCheckRollup":[]}'
                )
            )
            with patch.object(service.subprocess, "run", return_value=response):
                self.assertFalse(supervisor.pending_pull_request_is_open())

        self.assertNotIn("pending_factory_pr_url", supervisor.state)

    def test_pull_request_query_error_preserves_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.remember_pending_pull_request("https://github.com/example/repository/pull/1")
            with patch.object(
                service.subprocess,
                "run",
                side_effect=service.subprocess.TimeoutExpired("gh", 30),
            ):
                self.assertTrue(supervisor.pending_pull_request_is_open())

        self.assertIn("pending_factory_pr_url", supervisor.state)

    def test_malformed_pull_request_response_preserves_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))
            supervisor.remember_pending_pull_request("https://github.com/example/repository/pull/1")
            with patch.object(
                service.subprocess,
                "run",
                return_value=Mock(stdout='{"state":"UNKNOWN"}'),
            ):
                self.assertTrue(supervisor.pending_pull_request_is_open())

        self.assertIn("pending_factory_pr_url", supervisor.state)

    def test_partial_continuous_batch_waits_for_idle_interval(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))

            def conclusive_check():
                supervisor.update_check_conclusive = True
                supervisor.deployment_pending = False
                return False

            def stop_after_wait(_seconds):
                supervisor.stop_event.set()

            with (
                patch.object(service.signal, "signal"),
                patch.object(service, "prepare_git_authentication"),
                patch.object(supervisor, "check_for_update", side_effect=conclusive_check),
                patch.object(
                    supervisor,
                    "run_factory",
                    return_value=service.BatchOutcome(0, 3, "pr_open"),
                ),
                patch.object(supervisor, "wait", side_effect=stop_after_wait) as wait,
            ):
                supervisor.run()

        wait.assert_called_once_with(300)

    def test_failed_continuous_batch_uses_failure_backoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))

            def conclusive_check():
                supervisor.update_check_conclusive = True
                supervisor.deployment_pending = False
                return False

            def stop_after_wait(_seconds):
                supervisor.stop_event.set()

            with (
                patch.object(service.signal, "signal"),
                patch.object(service, "prepare_git_authentication"),
                patch.object(supervisor, "check_for_update", side_effect=conclusive_check),
                patch.object(
                    supervisor,
                    "run_factory",
                    return_value=service.BatchOutcome(1, None, "failed"),
                ),
                patch.object(supervisor, "wait", side_effect=stop_after_wait) as wait,
            ):
                supervisor.run()

        wait.assert_called_once_with(900)

    def test_auth_required_batch_persists_pause_and_starts_no_second_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))

            def conclusive_check():
                supervisor.update_check_conclusive = True
                supervisor.deployment_pending = False
                return False

            def stop_after_wait(_seconds):
                supervisor.stop_event.set()

            with (
                patch.object(service.signal, "signal"),
                patch.object(service, "prepare_git_authentication"),
                patch.object(supervisor, "check_for_update", side_effect=conclusive_check),
                patch.object(
                    supervisor,
                    "run_factory",
                    return_value=service.BatchOutcome(
                        1,
                        1,
                        "auth_required",
                        auth_reason_code="refresh_token_revoked",
                        auth_context={"source_id": 12},
                    ),
                ) as run_factory,
                patch.object(supervisor, "wait", side_effect=stop_after_wait),
            ):
                supervisor.run()

            self.assertEqual(run_factory.call_count, 1)
            self.assertTrue(supervisor.auth_pause.path.exists())

    def test_full_continuous_batch_with_pull_request_waits_before_next_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(self.config(Path(temporary)))

            def conclusive_check():
                supervisor.update_check_conclusive = True
                supervisor.deployment_pending = False
                return False

            def finish_batch(_now):
                return service.BatchOutcome(
                    0,
                    supervisor.config.max_urls,
                    "pr_open",
                    "https://github.com/example/repository/pull/1",
                    OLD_SHA,
                )

            poll_count = 0

            def stop_while_polling():
                nonlocal poll_count
                poll_count += 1
                if poll_count == 2:
                    supervisor.stop_event.set()
                    return True
                return False

            with (
                patch.object(service.signal, "signal"),
                patch.object(service, "prepare_git_authentication"),
                patch.object(supervisor, "check_for_update", side_effect=conclusive_check),
                patch.object(
                    supervisor,
                    "run_factory",
                    side_effect=finish_batch,
                ) as run_factory,
                patch.object(
                    supervisor,
                    "pending_pull_request_is_open",
                    side_effect=stop_while_polling,
                ),
                patch.object(supervisor, "wait") as wait,
            ):
                supervisor.run()

        self.assertEqual(run_factory.call_count, 1)
        self.assertEqual(
            supervisor.state["pending_factory_pr_url"],
            "https://github.com/example/repository/pull/1",
        )
        self.assertEqual(supervisor.state["pending_factory_pr_base_sha"], OLD_SHA)
        wait.assert_called_once_with(60)

    def test_scheduled_mode_does_not_run_while_previous_pull_request_is_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            supervisor = service.FactoryService(
                self.config(Path(temporary), mode=service.SCHEDULED_MODE)
            )
            supervisor.remember_pending_pull_request("https://github.com/example/repository/pull/1")

            def stop_after_wait(_seconds):
                supervisor.stop_event.set()

            with (
                patch.object(service.signal, "signal"),
                patch.object(service, "prepare_git_authentication"),
                patch.object(supervisor, "pending_pull_request_is_open", return_value=True),
                patch.object(supervisor, "run_factory") as run_factory,
                patch.object(supervisor, "wait", side_effect=stop_after_wait) as wait,
            ):
                supervisor.run()

        run_factory.assert_not_called()
        wait.assert_called_once_with(60)


if __name__ == "__main__":
    unittest.main()
