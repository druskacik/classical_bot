import json
import csv
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg2

import automation.run_crawler_factory as factory
from automation.crawler_registry import normalize_source_url, normalized_crawler_path
from automation.run_crawler_factory import (
    attempt_source,
    cleanup_untracked_scope_artifacts,
    crawler_directory,
    generated_geography,
    normalize_blocked_metadata,
    persist_failure_diagnostics,
    relocate_generated_directory,
    resolved_crawler_directory,
    parse_blocked_metadata,
    reconcile_pull_requests,
    validate_change_scope,
)
from automation.validate_crawler_pr import (
    PullRequestValidationError,
    generated_directories,
    validate_directory,
)
from build_crawlers_with_codex import crawler_folder_name, summarize_thread_items


def blocked_text(url: str, country_code: str, attempted: date) -> str:
    metadata = {
        "url": url,
        "geographic_scope": "country",
        "country_code": country_code,
        "reason_code": "no_current_events",
        "attempted_at": attempted.isoformat(),
        "retry_after": (attempted + timedelta(days=30)).isoformat(),
    }
    evidence = " ".join(["investigation evidence"] * 25)
    return (
        "<!-- crawler-factory-metadata\n"
        f"{json.dumps(metadata)}\n"
        "-->\n\n"
        f"# Blocked\n\n{evidence}\n"
    )


def unseeded_crawler_paths(
    existing_paths: set[str], seeded_paths: set[str]
) -> set[str]:
    seeded_slugs = {Path(path).name for path in seeded_paths}
    return {
        path for path in existing_paths if Path(path).name not in seeded_slugs
    }


class SupervisorResultTests(unittest.TestCase):
    def test_result_is_written_with_claim_and_outcome_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            factory.write_supervisor_result(
                path,
                run_id="run-1",
                claimed_count=2,
                results=[{"status": "generated"}, {"status": "blocked"}],
                status="pr_open",
                pull_request_url="https://example.test/pr/1",
                base_commit_sha="a" * 40,
            )
            result = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(result["claimed_count"], 2)
        self.assertEqual(result["attempted_count"], 2)
        self.assertEqual(result["status_counts"], {"blocked": 1, "generated": 1})
        self.assertEqual(result["status"], "pr_open")
        self.assertEqual(result["base_commit_sha"], "a" * 40)
        self.assertEqual(result["pull_request_url"], "https://example.test/pr/1")

    def test_database_cleanup_failure_does_not_interrupt_local_result_handling(self):
        registry = SimpleNamespace(
            transition_sources=Mock(),
            finish_run=Mock(side_effect=psycopg2.InterfaceError("closed")),
        )

        with self.assertLogs(factory.logger, level="ERROR") as captured:
            factory.persist_failure_cleanup(
                registry,
                "run-1",
                run_created=True,
                auth_reason_code=None,
                successful_source_ids=[],
            )

        registry.finish_run.assert_called_once_with("run-1", "failed")
        self.assertIn(
            "Could not persist crawler-factory failure cleanup",
            "\n".join(captured.output),
        )


class BlockedMetadataTests(unittest.TestCase):
    def test_valid_blocked_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "BLOCKED.md"
            today = date.today()
            path.write_text(blocked_text("https://example.cz/", "CZ", today), encoding="utf-8")

            result = parse_blocked_metadata(path)

        self.assertEqual(result["retry_after"], (today + timedelta(days=30)).isoformat())

    def test_worker_normalizes_blocked_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            url = "https://www.hamu.cz/"
            path = workspace / crawler_directory(url) / "BLOCKED.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "<!-- crawler-factory-metadata\n"
                '{"url":"https://hamu.cz","country_code":"CZ",'
                '"reason_code":"no_current_events","attempted_at":"2020-01-01",'
                '"retry_after":"2020-02-01"}\n'
                "-->\n\nThe source currently publishes no concerts.\n",
                encoding="utf-8",
            )
            attempted = date(2026, 7, 25)

            normalize_blocked_metadata(
                workspace,
                url,
                "country",
                "CZ",
                crawler_directory(url),
                attempted,
            )
            metadata = parse_blocked_metadata(path)

        self.assertEqual(metadata["url"], url)
        self.assertEqual(metadata["geographic_scope"], "country")
        self.assertEqual(metadata["attempted_at"], "2026-07-25")
        self.assertEqual(metadata["retry_after"], "2026-08-24")


class GeneratedGeographyTests(unittest.TestCase):
    def test_country_config_resolves_corrected_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "crawlers/de/example_es"
            directory.mkdir(parents=True)
            (directory / "main.py").write_text(
                'config = CrawlerConfig(country_code="es")\n',
                encoding="utf-8",
            )

            scope, country = generated_geography(directory)

        self.assertEqual((scope, country), ("country", "ES"))
        self.assertEqual(
            resolved_crawler_directory("example_es", scope, country),
            Path("crawlers/es/example_es"),
        )

    def test_none_config_resolves_common_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "crawlers/us/example_com"
            directory.mkdir(parents=True)
            (directory / "main.py").write_text(
                "config = CrawlerConfig(country_code=None)\n",
                encoding="utf-8",
            )

            scope, country = generated_geography(directory)

        self.assertEqual((scope, country), ("multi_country", None))
        self.assertEqual(
            resolved_crawler_directory("example_com", scope, country),
            Path("crawlers/common/example_com"),
        )

    def test_module_constant_resolves_country(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "crawlers/de/example_es"
            directory.mkdir(parents=True)
            (directory / "main.py").write_text(
                'COUNTRY_CODE = "es"\n'
                "config = CrawlerConfig(country_code=COUNTRY_CODE)\n",
                encoding="utf-8",
            )

            scope, country = generated_geography(directory)

        self.assertEqual((scope, country), ("country", "ES"))

    def test_annotated_module_constant_resolves_multi_country(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "crawlers/us/example_com"
            directory.mkdir(parents=True)
            (directory / "main.py").write_text(
                "COUNTRY_CODE: str | None = None\n"
                "config = CrawlerConfig(country_code=COUNTRY_CODE)\n",
                encoding="utf-8",
            )

            scope, country = generated_geography(directory)

        self.assertEqual((scope, country), ("multi_country", None))

    def test_unresolved_country_constant_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "crawlers/de/example_de"
            directory.mkdir(parents=True)
            (directory / "main.py").write_text(
                "config = CrawlerConfig(country_code=COUNTRY_CODE)\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unresolved module constant"):
                generated_geography(directory)

    def test_computed_country_constant_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "crawlers/de/example_de"
            directory.mkdir(parents=True)
            (directory / "main.py").write_text(
                'COUNTRY_CODE = "de".upper()\n'
                "config = CrawlerConfig(country_code=COUNTRY_CODE)\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unresolved module constant"):
                generated_geography(directory)

    def test_relocation_preserves_generated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            current = Path("crawlers/de/example_es")
            final = Path("crawlers/es/example_es")
            path = workspace / current / "main.py"
            path.parent.mkdir(parents=True)
            path.write_text("generated\n", encoding="utf-8")

            relocate_generated_directory(workspace, current, final)

            self.assertFalse((workspace / current).exists())
            self.assertEqual(
                (workspace / final / "main.py").read_text(encoding="utf-8"),
                "generated\n",
            )


class RegistryIdentityTests(unittest.TestCase):
    def test_url_identity_ignores_scheme_www_and_fragment(self):
        self.assertEqual(
            normalize_source_url("http://www.Example.cz/#programme"),
            "https://example.cz/",
        )

    def test_url_identity_preserves_meaningful_path_and_query(self):
        self.assertEqual(
            normalize_source_url("https://example.cz/events?season=2026#top"),
            "https://example.cz/events?season=2026",
        )

    def test_crawler_path_must_be_scoped(self):
        self.assertEqual(
            normalized_crawler_path("crawlers/cz/example_cz"),
            "crawlers/cz/example_cz",
        )
        with self.assertRaisesRegex(ValueError, "crawlers/<country>/<slug>"):
            normalized_crawler_path("example_cz")

    def test_legacy_seed_contains_salvator_alias_and_unique_urls(self):
        seed = (
            Path(__file__).parents[1]
            / "seeds/crawler_sources/0001_legacy_builder_urls.csv"
        )
        with seed.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        urls = [row["url"] for row in rows]
        self.assertEqual(len(urls), len(set(urls)))
        salvator = next(row for row in rows if "salvator.farnost.cz" in row["url"])
        self.assertEqual(
            salvator["canonical_url"],
            "https://www.farnostsalvator.cz/",
        )
        self.assertEqual(
            salvator["crawler_path"],
            "crawlers/cz/farnostsalvator_cz",
        )

    def test_versioned_seeds_cover_every_existing_crawler_directory(self):
        root = Path(__file__).parents[1]
        seeded_paths = set()
        for seed in sorted((root / "seeds/crawler_sources").glob("*.csv")):
            with seed.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    path = row["crawler_path"]
                    if not path:
                        source_url = row["canonical_url"] or row["url"]
                        path = (
                            f"crawlers/{row['country_code'].lower()}/"
                            f"{crawler_folder_name(source_url)}"
                        )
                    seeded_paths.add(path)
        existing_paths = {
            marker.parent.relative_to(root).as_posix()
            for marker in (
                list((root / "crawlers").glob("*/*/main.py"))
                + list((root / "crawlers").glob("*/*/BLOCKED.md"))
            )
        }
        missing_paths = unseeded_crawler_paths(existing_paths, seeded_paths)
        self.assertFalse(missing_paths, missing_paths)
        with (
            root / "seeds/crawler_sources/0002_existing_crawlers.csv"
        ).open(newline="", encoding="utf-8") as handle:
            existing_seed_paths = {
                row["crawler_path"] for row in csv.DictReader(handle)
            }
        self.assertTrue(
            existing_seed_paths.issubset(existing_paths),
            existing_seed_paths - existing_paths,
        )

    def test_seed_coverage_allows_resolved_country_relocation(self):
        seeded_paths = {"crawlers/fr/ensemblevocalorphee_com"}

        self.assertFalse(
            unseeded_crawler_paths(
                {"crawlers/ch/ensemblevocalorphee_com"}, seeded_paths
            )
        )
        self.assertEqual(
            unseeded_crawler_paths(
                {"crawlers/ch/unrelated_example"}, seeded_paths
            ),
            {"crawlers/ch/unrelated_example"},
        )


class FactoryArgumentTests(unittest.TestCase):
    def test_model_can_be_overridden_per_run(self):
        with (
            patch.dict(
                "os.environ",
                {"CRAWLER_FACTORY_REPOSITORY": "https://example.test/repository.git"},
                clear=False,
            ),
            patch.object(
                sys,
                "argv",
                ["run_crawler_factory", "--model", "gpt-5.6-luna"],
            ),
        ):
            args = factory.parse_args()
        self.assertEqual(args.model, "gpt-5.6-luna")

    def test_validation_timeout_can_be_overridden_per_run(self):
        with (
            patch.dict(
                "os.environ",
                {"CRAWLER_FACTORY_REPOSITORY": "https://example.test/repository.git"},
                clear=False,
            ),
            patch.object(
                sys,
                "argv",
                ["run_crawler_factory", "--validation-timeout-minutes", "45"],
            ),
        ):
            args = factory.parse_args()
        self.assertEqual(args.validation_timeout_minutes, 45)

    def test_prompt_contains_generated_record_quality_contract(self):
        prompt = (
            Path(__file__).parents[1] / "prompts/build_crawler.mustache"
        ).read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        self.assertIn("Start and end times and descriptions may be None", normalized)
        self.assertNotIn("country_code, type, description", normalized)
        self.assertIn("all in-scope concerts", normalized)
        self.assertIn("including past concerts", normalized)
        self.assertIn("must never be empty", normalized)
        self.assertIn("upload_target='classical'", normalized)
        self.assertIn("upload_target` to `potential`", normalized)
        self.assertIn("first-party genre, category, discipline, event-type", normalized)
        self.assertIn("`prompts/event_inclusion_guidance.mustache`", normalized)
        self.assertIn("A mixed source may also use `classical`", normalized)
        self.assertIn("filtering is unavailable, incomplete, ambiguous", normalized)
        self.assertIn("Do not upload an unfiltered mixed-source feed", normalized)
        self.assertIn("eligible opera, operetta, ballet or classical dance", normalized)
        self.assertIn("soundtrack concerts, classical crossover", normalized)
        self.assertIn("which first-party filters and exact values you tested", normalized)
        self.assertIn("whether they persisted across pagination", normalized)
        self.assertIn("inspect `crawlers/base.py`", normalized)
        self.assertIn("Start with this minimal valid configuration", normalized)
        self.assertIn('slug="site_domain"', normalized)
        self.assertIn(
            "The required constructor fields are `slug`, `source`, and `source_url`",
            normalized,
        )
        self.assertIn("This example is intentionally minimal", normalized)
        self.assertIn(
            "are not automatically copied into scraped records",
            normalized,
        )
        self.assertIn(
            "`front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)]`",
            normalized,
        )
        self.assertIn("`csv_path`", normalized)
        self.assertIn("unsupported aliases such as `name` or `csv_filename`", normalized)
        self.assertIn("cheap import and instantiation check", normalized)

    def test_repository_defaults_to_environment(self):
        with (
            patch.dict(
                "os.environ",
                {"CRAWLER_FACTORY_REPOSITORY": "https://example.test/repository.git"},
                clear=False,
            ),
            patch.object(sys, "argv", ["run_crawler_factory"]),
        ):
            args = factory.parse_args()
        self.assertEqual(
            args.repository,
            "https://example.test/repository.git",
        )

    def test_repository_flag_overrides_environment(self):
        with (
            patch.dict(
                "os.environ",
                {"CRAWLER_FACTORY_REPOSITORY": "https://example.test/default.git"},
                clear=False,
            ),
            patch.object(
                sys,
                "argv",
                [
                    "run_crawler_factory",
                    "--repository",
                    "https://example.test/override.git",
                ],
            ),
        ):
            args = factory.parse_args()
        self.assertEqual(
            args.repository,
            "https://example.test/override.git",
        )


class PullRequestReconciliationTests(unittest.TestCase):
    class Registry:
        def __init__(self):
            self.transitions = []

        def pr_open_sources(self):
            return [
                {
                    "id": 1,
                    "crawler_path": "crawlers/cz/example_cz",
                    "pull_request_url": "https://github.com/example/repo/pull/1",
                }
            ]

        def transition_sources(self, source_ids, status, retry_after=None):
            self.transitions.append((source_ids, status, retry_after))

    def test_closed_unmerged_pr_retries(self):
        registry = self.Registry()
        response = subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"state": "CLOSED", "mergedAt": None, "statusCheckRollup": []}),
            "",
        )
        with patch.object(factory, "run_command", return_value=response):
            counts = reconcile_pull_requests(registry, Path.cwd())
        self.assertEqual(counts["retry_wait"], 1)
        self.assertEqual(registry.transitions[0][1], "retry_wait")

    def test_failed_open_pr_needs_attention(self):
        registry = self.Registry()
        response = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "state": "OPEN",
                    "mergedAt": None,
                    "statusCheckRollup": [{"conclusion": "FAILURE"}],
                }
            ),
            "",
        )
        with patch.object(factory, "run_command", return_value=response):
            counts = reconcile_pull_requests(registry, Path.cwd())
        self.assertEqual(counts["needs_attention"], 1)
        self.assertEqual(registry.transitions[0][1], "needs_attention")


class ScopeTests(unittest.TestCase):
    def test_expected_main_file_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            expected = Path("crawlers/cz/example_cz")
            path = workspace / expected / "main.py"
            path.parent.mkdir(parents=True)
            path.write_text("# generated\n", encoding="utf-8")

            validate_change_scope(workspace, expected)

    def test_unrelated_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            expected = Path("crawlers/cz/example_cz")
            path = workspace / expected / "main.py"
            path.parent.mkdir(parents=True)
            path.write_text("# generated\n", encoding="utf-8")
            (workspace / "pyproject.toml").write_text("# changed\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "outside the allowed scope"):
                validate_change_scope(workspace, expected)

    def test_main_and_blocked_together_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            expected = Path("crawlers/cz/example_cz")
            directory = workspace / expected
            directory.mkdir(parents=True)
            (directory / "main.py").write_text("# generated\n", encoding="utf-8")
            (directory / "BLOCKED.md").write_text("blocked\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                validate_change_scope(workspace, expected)

    def test_untracked_scratch_file_is_removed_and_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            expected = Path("crawlers/at/example_at")
            main_path = workspace / expected / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text("# generated\n", encoding="utf-8")
            scratch = workspace / "investigation.html"
            scratch.write_text("<html></html>\n", encoding="utf-8")
            report_path = root / "scope-report.json"

            removed = cleanup_untracked_scope_artifacts(
                workspace, expected, report_path
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(removed, ["investigation.html"])
            self.assertFalse(scratch.exists())
            self.assertEqual(report["removed_untracked_paths"], removed)
            validate_change_scope(workspace, expected)

    def test_tracked_out_of_scope_change_is_not_cleaned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            tracked = workspace / "pyproject.toml"
            tracked.write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "pyproject.toml"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"],
                cwd=workspace,
                check=True,
            )
            expected = Path("crawlers/at/example_at")
            main_path = workspace / expected / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text("# generated\n", encoding="utf-8")
            tracked.write_text("modified\n", encoding="utf-8")
            report_path = root / "scope-report.json"

            removed = cleanup_untracked_scope_artifacts(
                workspace, expected, report_path
            )

            self.assertEqual(removed, [])
            self.assertEqual(tracked.read_text(encoding="utf-8"), "modified\n")
            with self.assertRaisesRegex(RuntimeError, "outside the allowed scope"):
                validate_change_scope(workspace, expected)

    def test_untracked_symlink_cleanup_does_not_touch_external_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            expected = Path("crawlers/at/example_at")
            main_path = workspace / expected / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text("# generated\n", encoding="utf-8")
            external = root / "external.html"
            external.write_text("keep me\n", encoding="utf-8")
            scratch_link = workspace / "scratch.html"
            scratch_link.symlink_to(external)

            cleanup_untracked_scope_artifacts(
                workspace, expected, root / "scope-report.json"
            )

            self.assertFalse(scratch_link.exists())
            self.assertEqual(external.read_text(encoding="utf-8"), "keep me\n")

    def test_failure_diagnostics_preserve_generated_crawler_and_tracked_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "run"
            workspace.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            tracked = workspace / "pyproject.toml"
            tracked.write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "pyproject.toml"], cwd=workspace, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qm",
                    "initial",
                ],
                cwd=workspace,
                check=True,
            )
            crawler = Path("crawlers/at/example_at")
            generated = workspace / crawler / "main.py"
            generated.parent.mkdir(parents=True)
            generated.write_text("# generated\n", encoding="utf-8")
            tracked.write_text("modified\n", encoding="utf-8")

            diagnostic_dir = persist_failure_diagnostics(
                workspace, run_dir, {"id": 83}, crawler
            )

            self.assertTrue((diagnostic_dir / "generated-crawler/main.py").exists())
            self.assertIn(
                "pyproject.toml",
                (diagnostic_dir / "tracked.patch").read_text(encoding="utf-8"),
            )
            self.assertTrue((diagnostic_dir / "git-status.json").exists())


class BuilderItemSummaryTests(unittest.TestCase):
    def test_keeps_activity_metadata_without_outputs_or_arguments(self):
        items = [
            SimpleNamespace(
                type="commandExecution",
                id="command-1",
                command="API_TOKEN=secret curl -H 'Authorization: Bearer hidden' https://example.com > /tmp/page.html",
                cwd="/workspace",
                status="completed",
                exit_code=0,
                duration_ms=12,
                aggregated_output="sensitive page contents",
            ),
            SimpleNamespace(
                type="mcpToolCall",
                id="tool-1",
                server="playwright",
                tool="navigate",
                status="completed",
                duration_ms=20,
                arguments={"token": "secret"},
                result="page contents",
            ),
        ]

        summaries = summarize_thread_items(items)

        self.assertIn("[REDACTED]", summaries[0]["command"])
        self.assertNotIn("secret", summaries[0]["command"])
        self.assertNotIn("hidden", summaries[0]["command"])
        self.assertEqual(summaries[1]["tool"], "navigate")
        self.assertNotIn("aggregated_output", summaries[0])
        self.assertNotIn("arguments", summaries[1])
        self.assertNotIn("result", summaries[1])


class AttemptTests(unittest.TestCase):
    SOURCE = {
        "id": 12,
        "canonical_url": "https://www.hamu.cz/",
        "country_code": "CZ",
        "crawler_path": "crawlers/cz/hamu_cz",
    }

    def test_builder_auth_failure_is_reported_before_scope_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "runs"
            workspace.mkdir()
            run_dir.mkdir()

            def fake_run_command(command, **_kwargs):
                report = Path(command[command.index("--results") + 1])
                report.write_text(
                    json.dumps(
                        [{
                            "status": "auth_required",
                            "auth_reason_code": "refresh_token_revoked",
                            "error": "Codex authentication is required",
                        }]
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 1, "", "")

            with patch.object(factory, "run_command", side_effect=fake_run_command):
                result = attempt_source(workspace, run_dir, self.SOURCE, 30, {})

        self.assertEqual(result["status"], "auth_required")
        self.assertEqual(result["auth_reason_code"], "refresh_token_revoked")
        self.assertEqual(result["failure_stage"], "builder")

    def test_builder_error_is_preserved_when_no_changes_are_produced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "runs"
            workspace.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            original_run_command = factory.run_command

            def fake_run_command(command, **kwargs):
                if any(str(part).endswith("build_crawlers_with_codex.py") for part in command):
                    report = Path(command[command.index("--results") + 1])
                    report.write_text(
                        json.dumps(
                            [{
                                "status": "generation_failed",
                                "error": "Selected model is at capacity",
                            }]
                        ),
                        encoding="utf-8",
                    )
                    (workspace / "scratch.html").write_text("temporary", encoding="utf-8")
                    return subprocess.CompletedProcess(command, 1, "", "")
                return original_run_command(command, **kwargs)

            with patch.object(factory, "run_command", side_effect=fake_run_command):
                result = attempt_source(workspace, run_dir, self.SOURCE, 30, {})

        self.assertEqual(result["status"], "generation_failed")
        self.assertEqual(result["builder_status"], "generation_failed")
        self.assertEqual(result["builder_error"], "Selected model is at capacity")
        self.assertEqual(result["error"], "Selected model is at capacity")
        self.assertEqual(result["failure_stage"], "builder")
        self.assertEqual(result["generation_warning"], "builder exited with status 1")
        self.assertFalse((workspace / "scratch.html").exists())

    def test_untracked_scratch_artifact_is_cleaned_before_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "runs"
            workspace.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            original_run_command = factory.run_command

            def fake_run_command(command, **kwargs):
                if any(str(part).endswith("build_crawlers_with_codex.py") for part in command):
                    path = workspace / self.SOURCE["crawler_path"] / "main.py"
                    path.parent.mkdir(parents=True)
                    path.write_text(
                        'from crawlers.base import CrawlerConfig\n'
                        'config = CrawlerConfig(slug="hamu_cz", source="HAMU", '
                        'source_url="https://www.hamu.cz/", country_code="CZ")\n',
                        encoding="utf-8",
                    )
                    (workspace / "scratch.html").write_text("scratch\n", encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if "automation.validate_generated_crawler" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "status": "passed",
                                "record_count": 2,
                                "issue_count": 0,
                                "issues": [],
                                "duration_seconds": 0.2,
                            }
                        ),
                        "",
                    )
                return original_run_command(command, **kwargs)

            with (
                patch.object(factory, "run_command", side_effect=fake_run_command),
                patch.object(factory, "git_commit", return_value="abc123"),
                self.assertLogs(factory.logger, level="WARNING") as captured,
            ):
                result = attempt_source(
                    workspace,
                    run_dir,
                    self.SOURCE,
                    30,
                    {},
                    run_id="run-1",
                    attempt_id=99,
                )

            scope_report = json.loads(
                Path(result["scope_report_path"]).read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "generated")
        self.assertEqual(scope_report["removed_untracked_paths"], ["scratch.html"])
        self.assertTrue(
            any(
                getattr(record, "event", None) == "factory_source_scope_cleanup"
                and record.run_id == "run-1"
                and record.source_id == 12
                for record in captured.records
            )
        )

    def test_generated_country_correction_relocates_before_commit(self):
        source = {
            "id": 13,
            "canonical_url": "https://example.com/",
            "country_code": "DE",
            "geographic_scope": "unknown",
            "crawler_path": "crawlers/de/example_com",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "runs"
            workspace.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            original_run_command = factory.run_command
            resolved = []

            def fake_run_command(command, **kwargs):
                if any(str(part).endswith("build_crawlers_with_codex.py") for part in command):
                    path = workspace / source["crawler_path"] / "main.py"
                    path.parent.mkdir(parents=True)
                    path.write_text(
                        'config = CrawlerConfig(country_code="ES")\n',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "", "")
                if "automation.validate_generated_crawler" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "status": "passed",
                                "record_count": 1,
                                "issue_count": 0,
                                "issues": [],
                                "duration_seconds": 0.1,
                            }
                        ),
                        "",
                    )
                return original_run_command(command, **kwargs)

            def resolve_identity(current, scope, country, path):
                resolved.append((scope, country, path))
                return {
                    **current,
                    "country_code": country,
                    "geographic_scope": scope,
                    "crawler_path": path,
                }

            with (
                patch.object(factory, "run_command", side_effect=fake_run_command),
                patch.object(factory, "git_commit", return_value="abc123"),
            ):
                result = attempt_source(
                    workspace,
                    run_dir,
                    source,
                    30,
                    {},
                    identity_resolver=resolve_identity,
                )

            self.assertFalse((workspace / "crawlers/de/example_com").exists())
            self.assertTrue((workspace / "crawlers/es/example_com/main.py").exists())

        self.assertEqual(resolved, [("country", "ES", "crawlers/es/example_com")])
        self.assertEqual(result["status"], "generated")
        self.assertEqual(result["crawler_directory"], "crawlers/es/example_com")

    def test_nonzero_builder_result_is_preserved_when_scope_is_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "runs"
            workspace.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            original_run_command = factory.run_command

            def fake_run_command(command, **kwargs):
                if any(str(part).endswith("build_crawlers_with_codex.py") for part in command):
                    self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
                    self.assertEqual(
                        command[command.index("--sandbox") + 1],
                        "full-access",
                    )
                    path = workspace / crawler_directory("https://www.hamu.cz/") / "main.py"
                    path.parent.mkdir(parents=True)
                    path.write_text(
                        'from crawlers.base import CrawlerConfig\n'
                        'config = CrawlerConfig(slug="hamu_cz", source="HAMU", '
                        'source_url="https://www.hamu.cz/", country_code="CZ")\n',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 1, "", "builder failed")
                if "automation.validate_generated_crawler" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "status": "passed",
                                "record_count": 2,
                                "issue_count": 0,
                                "issues": [],
                                "duration_seconds": 1.5,
                            }
                        ),
                        "",
                    )
                return original_run_command(command, **kwargs)

            with (
                patch.object(factory, "run_command", side_effect=fake_run_command),
                patch.object(factory, "git_commit", return_value="abc123"),
            ):
                result = attempt_source(
                    workspace,
                    run_dir,
                    self.SOURCE,
                    30,
                    {},
                    "gpt-5.6-luna",
                )

        self.assertEqual(result["status"], "generated")
        self.assertEqual(result["commit"], "abc123")
        self.assertEqual(result["generation_warning"], "builder exited with status 1")
        self.assertEqual(result["validation_status"], "passed")

    def test_timed_out_builder_result_is_preserved_when_scope_is_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "runs"
            workspace.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            original_run_command = factory.run_command

            def fake_run_command(command, **kwargs):
                if any(str(part).endswith("build_crawlers_with_codex.py") for part in command):
                    path = workspace / crawler_directory("https://www.hamu.cz/") / "main.py"
                    path.parent.mkdir(parents=True)
                    path.write_text(
                        'from crawlers.base import CrawlerConfig\n'
                        'config = CrawlerConfig(slug="hamu_cz", source="HAMU", '
                        'source_url="https://www.hamu.cz/", country_code="CZ")\n',
                        encoding="utf-8",
                    )
                    raise subprocess.TimeoutExpired(command, 60, output="partial log\n")
                if "automation.validate_generated_crawler" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            {
                                "status": "passed",
                                "record_count": 1,
                                "issue_count": 0,
                                "issues": [],
                                "duration_seconds": 2.0,
                            }
                        ),
                        "",
                    )
                return original_run_command(command, **kwargs)

            with (
                patch.object(factory, "run_command", side_effect=fake_run_command),
                patch.object(factory, "git_commit", return_value="abc123"),
            ):
                result = attempt_source(
                    workspace,
                    run_dir,
                    self.SOURCE,
                    1,
                    {},
                )

        self.assertEqual(result["status"], "generated")
        self.assertEqual(result["commit"], "abc123")
        self.assertEqual(result["generation_warning"], "builder exceeded 1 minute")

    def test_failed_live_validation_prevents_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            run_dir = root / "runs"
            workspace.mkdir()
            run_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)

            def fake_run_command(command, **kwargs):
                if any(str(part).endswith("build_crawlers_with_codex.py") for part in command):
                    path = workspace / crawler_directory("https://www.hamu.cz/") / "main.py"
                    path.parent.mkdir(parents=True)
                    path.write_text(
                        'from crawlers.base import CrawlerConfig\n'
                        'config = CrawlerConfig(slug="hamu_cz", source="HAMU", '
                        'source_url="https://www.hamu.cz/", country_code="CZ")\n',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[:2] == ["git", "status"]:
                    path = crawler_directory("https://www.hamu.cz/") / "main.py"
                    return subprocess.CompletedProcess(command, 0, f"?? {path}\0", "")
                if "automation.validate_generated_crawler" in command:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        json.dumps(
                            {
                                "status": "data_quality_failure",
                                "record_count": 2,
                                "issue_count": 1,
                                "issues": [
                                    {
                                        "record": 1,
                                        "field": "venue",
                                        "reason": "required nonempty venue",
                                        "value": None,
                                    }
                                ],
                                "duration_seconds": 1.0,
                            }
                        ),
                        "",
                    )
                raise AssertionError(command)

            with (
                patch.object(factory, "run_command", side_effect=fake_run_command),
                patch.object(factory, "git_commit") as commit,
            ):
                result = attempt_source(
                    workspace,
                    run_dir,
                    self.SOURCE,
                    30,
                    {},
                )

        self.assertEqual(result["status"], "generation_failed")
        self.assertEqual(result["validation_status"], "data_quality_failure")
        self.assertIn("venue", result["error"])
        commit.assert_not_called()

    def test_live_validation_timeout_is_inconclusive_and_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with patch.object(
                factory,
                "run_command",
                side_effect=subprocess.TimeoutExpired(["python"], 60),
            ):
                report = factory.validate_generated_crawler(
                    Path(temporary),
                    run_dir,
                    self.SOURCE,
                    crawler_directory("https://www.hamu.cz/"),
                    {},
                    1,
                )

            reports = list(run_dir.glob("*-validation.json"))

        self.assertEqual(report["status"], "inconclusive_runtime")
        self.assertEqual(len(reports), 1)


class PullRequestScopeTests(unittest.TestCase):
    def initialize_repository(self, workspace: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=workspace, check=True)
        (workspace / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-q",
                "-m",
                "base",
            ],
            cwd=workspace,
            check=True,
        )

    def commit(self, workspace: Path, message: str) -> None:
        subprocess.run(["git", "add", "."], cwd=workspace, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-q",
                "-m",
                message,
            ],
            cwd=workspace,
            check=True,
        )

    def test_new_crawler_directory_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self.initialize_repository(workspace)
            subprocess.run(["git", "switch", "-q", "-c", "crawler-factory/test"], cwd=workspace, check=True)
            main_path = workspace / "crawlers/cz/example_cz/main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text("# generated\n", encoding="utf-8")
            self.commit(workspace, "generated")

            directories = generated_directories(workspace, "master")

        self.assertEqual(directories, [Path("crawlers/cz/example_cz")])

    def test_existing_crawler_modification_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self.initialize_repository(workspace)
            main_path = workspace / "crawlers/cz/example_cz/main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text("# original\n", encoding="utf-8")
            self.commit(workspace, "existing crawler")
            subprocess.run(["git", "switch", "-q", "-c", "crawler-factory/test"], cwd=workspace, check=True)
            main_path.write_text("# changed\n", encoding="utf-8")
            self.commit(workspace, "modify crawler")

            with self.assertRaisesRegex(PullRequestValidationError, "existing crawler"):
                generated_directories(workspace, "master")

    def test_valid_python_is_compiled_without_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/cz/example_cz")
            main_path = workspace / directory / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text(
                "class Crawler:\n"
                "    def run(self):\n"
                "        raise RuntimeError('must not execute')\n"
                "def main():\n"
                "    Crawler().run()\n"
                "raise RuntimeError('must not execute')\n",
                encoding="utf-8",
            )

            result = validate_directory(workspace, directory)

        self.assertEqual(result, {"status": "passed", "kind": "crawler"})

    def test_common_crawler_requires_no_default_country(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/common/example_com")
            main_path = workspace / directory / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text(
                "config = CrawlerConfig(country_code=None)\n"
                "def main():\n"
                "    ExampleCrawler().run()\n",
                encoding="utf-8",
            )

            result = validate_directory(workspace, directory)

        self.assertEqual(result, {"status": "passed", "kind": "crawler"})

    def test_common_crawler_accepts_annotated_none_constant(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/common/example_com")
            main_path = workspace / directory / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text(
                "COUNTRY_CODE: str | None = None\n"
                "config = CrawlerConfig(country_code=COUNTRY_CODE)\n"
                "def main():\n"
                "    ExampleCrawler().run()\n",
                encoding="utf-8",
            )

            result = validate_directory(workspace, directory)

        self.assertEqual(result, {"status": "passed", "kind": "crawler"})

    def test_unresolved_country_constant_is_rejected_by_pr_validator(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/cz/example_cz")
            main_path = workspace / directory / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text(
                "config = CrawlerConfig(country_code=COUNTRY_CODE)\n"
                "def main():\n"
                "    ExampleCrawler().run()\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                PullRequestValidationError, "literal ISO code or None"
            ):
                validate_directory(workspace, directory)

    def test_main_may_run_a_named_crawler_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/cz/example_cz")
            main_path = workspace / directory / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text(
                "def main():\n"
                "    crawler = ExampleCrawler()\n"
                "    crawler.run()\n",
                encoding="utf-8",
            )

            result = validate_directory(workspace, directory)

        self.assertEqual(result, {"status": "passed", "kind": "crawler"})

    def test_missing_main_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/cz/example_cz")
            main_path = workspace / directory / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text("ExampleCrawler().run()\n", encoding="utf-8")

            with self.assertRaisesRegex(PullRequestValidationError, "top-level main"):
                validate_directory(workspace, directory)

    def test_scrape_only_main_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/cz/example_cz")
            main_path = workspace / directory / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text(
                "def main():\n"
                "    concerts = ExampleCrawler().scrape()\n"
                "    print(concerts)\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PullRequestValidationError, "must call.*run"):
                validate_directory(workspace, directory)

    def test_every_existing_crawler_has_a_persisting_main(self):
        root = Path(__file__).parents[1]
        for main_path in sorted((root / "crawlers").glob("*/*/main.py")):
            with self.subTest(crawler=main_path.parent.relative_to(root)):
                validate_directory(root, main_path.parent.relative_to(root))

    def test_generated_crawler_print_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/cz/example_cz")
            main_path = workspace / directory / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text(
                "def main():\n"
                "    print('starting')\n"
                "    ExampleCrawler().run()\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PullRequestValidationError, "structured logging"):
                validate_directory(workspace, directory)

    def test_invalid_python_syntax_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/cz/example_cz")
            main_path = workspace / directory / "main.py"
            main_path.parent.mkdir(parents=True)
            main_path.write_text("def broken(:\n", encoding="utf-8")

            with self.assertRaisesRegex(PullRequestValidationError, "invalid Python syntax"):
                validate_directory(workspace, directory)

    def test_blocked_result_does_not_require_live_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            directory = Path("crawlers/cz/example_cz")
            blocked_path = workspace / directory / "BLOCKED.md"
            blocked_path.parent.mkdir(parents=True)
            blocked_path.write_text("Temporarily unavailable.\n", encoding="utf-8")

            result = validate_directory(workspace, directory)

        self.assertEqual(result, {"status": "passed", "kind": "blocked"})


if __name__ == "__main__":
    unittest.main()
