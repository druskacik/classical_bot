import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from analyzers import analyze_potential_events as analyzer


class PotentialEventAnalyzerTests(unittest.TestCase):
    def event(self, event_id: int, **overrides):
        values = {
            "id": event_id,
            "title": "String Quartet Plays Radiohead",
            "date": date(2026, 9, 1),
            "url": f"https://example.test/events/{event_id}",
            "source": "Example Hall",
            "source_url": "https://example.test",
            "time_from": None,
            "time_to": None,
            "city_raw": "Prague",
            "country_code_raw": "CZ",
            "venue": "Main Hall",
            "type": "concert",
            "description": "A string quartet performs classical arrangements.",
        }
        values.update(overrides)
        return analyzer.PotentialEvent(**values)

    def test_exact_classification_inputs_are_grouped(self):
        groups = analyzer.candidate_groups([self.event(1), self.event(2)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].event_ids, [1, 2])

    def test_changed_description_is_not_grouped(self):
        groups = analyzer.candidate_groups(
            [self.event(1), self.event(2, description="A rock band performs.")]
        )
        self.assertEqual(len(groups), 2)

    def test_pages_obey_event_limit_and_split_large_groups(self):
        group = analyzer.candidate_groups([self.event(index) for index in range(1, 8)])[0]
        pages = analyzer.pack_pages(
            [group], maximum_events=3, maximum_chars=1_000_000
        )
        self.assertEqual(
            [[event_id for item in page for event_id in item.event_ids] for page in pages],
            [[1, 2, 3], [4, 5, 6], [7]],
        )

    def test_pages_obey_character_budget_between_groups(self):
        groups = analyzer.candidate_groups(
            [
                self.event(1, title="One", description="a" * 200),
                self.event(2, title="Two", description="b" * 200),
            ]
        )
        pages = analyzer.pack_pages(groups, maximum_events=100, maximum_chars=300)
        self.assertEqual(len(pages), 2)

    def valid_result(self):
        return {
            "classifications": [
                {
                    "event_ids": [1, 2],
                    "decision": "classical",
                    "category": "soundtrack_game_or_crossover",
                    "rationale": "Classical string-quartet performance format.",
                    "evidence_urls": ["https://example.test/events/1"],
                }
            ],
            "source_findings": [],
        }

    def test_result_requires_exact_id_coverage(self):
        page = analyzer.candidate_groups([self.event(1), self.event(2)])
        analyzer.validate_result(page, self.valid_result())
        missing = self.valid_result()
        missing["classifications"][0]["event_ids"] = [1]
        with self.assertRaisesRegex(ValueError, "missing classifications"):
            analyzer.validate_result(page, missing)

    def test_result_rejects_incompatible_decision_category(self):
        page = analyzer.candidate_groups([self.event(1), self.event(2)])
        result = self.valid_result()
        result["classifications"][0]["category"] = "commercial_musical_theatre"
        with self.assertRaisesRegex(ValueError, "incompatible category"):
            analyzer.validate_result(page, result)

    def test_result_requires_nonempty_evidence(self):
        page = analyzer.candidate_groups([self.event(1), self.event(2)])
        result = self.valid_result()
        result["classifications"][0]["evidence_urls"] = []
        with self.assertRaisesRegex(ValueError, "at least one evidence URL"):
            analyzer.validate_result(page, result)

    def test_non_event_finding_requires_not_event_decision(self):
        page = analyzer.candidate_groups([self.event(1), self.event(2)])
        result = self.valid_result()
        result["source_findings"] = [
            {
                "code": "non_event_ingestion",
                "severity": "error",
                "event_ids": [1, 2],
                "summary": "This is an ensemble membership page.",
                "evidence_urls": ["https://example.test/events/1"],
            }
        ]
        with self.assertRaisesRegex(ValueError, "require not_event"):
            analyzer.validate_result(page, result)

        result["classifications"][0].update(
            decision="not_event",
            category="membership_course_or_rehearsal",
        )
        analyzer.validate_result(page, result)

    def test_prompt_uses_nuanced_musical_boundary(self):
        page = analyzer.candidate_groups(
            [self.event(1, title="West Side Story", type="musical")]
        )
        prompt = analyzer.render_prompt(
            source="Example Hall",
            source_url="https://example.test",
            page=page,
            page_number=1,
            page_count=1,
        )
        self.assertIn("West Side Story", prompt)
        self.assertIn("not automatically", prompt)
        self.assertIn("commercial musical-theatre", prompt)
        guidance = analyzer.load_inclusion_guidance()
        self.assertEqual(prompt.count(guidance), 1)
        normalized_prompt = " ".join(prompt.split())
        self.assertIn("Named canonical repertoire is not required", normalized_prompt)
        self.assertIn("billed symphonic collaborations with modern artists", normalized_prompt)
        self.assertIn("Family and children's labels are neutral", normalized_prompt)
        self.assertIn("contemporary dance or ballet", normalized_prompt)
        self.assertIn("Seasonal concerts by", normalized_prompt)
        self.assertIn("Vague marketing", normalized_prompt)
        self.assertIn("explicitly bills an orchestra, string trio", normalized_prompt)
        self.assertIn("organ-and-voice concert", normalized_prompt)
        self.assertIn("a vocalist, drums, bass, or another rhythm section", normalized_prompt)
        self.assertIn("Kurt Weill song programme", normalized_prompt)

    def test_rendered_prompt_categories_come_from_schema_definitions(self):
        prompt = analyzer.render_prompt(
            source="Example Hall",
            source_url="https://example.test",
            page=analyzer.candidate_groups([self.event(1)]),
            page_number=1,
            page_count=1,
        )
        schema_categories = analyzer.OUTPUT_SCHEMA["properties"]["classifications"][
            "items"
        ]["properties"]["category"]["enum"]

        self.assertEqual(schema_categories, analyzer.ALL_CATEGORIES)
        prompt_lines = prompt.splitlines()
        for category in analyzer.ALL_CATEGORIES:
            self.assertEqual(prompt_lines.count(f"- {category}"), 1)
        self.assertNotIn("{{#classical_categories}}", prompt)
        self.assertNotIn("{{#nonclassical_categories}}", prompt)
        self.assertNotIn("{{uncertain_category}}", prompt)

    def test_automatic_selection_excludes_past_events(self):
        automatic = analyzer.eligibility_sql(include_past=False, force=False)
        historical = analyzer.eligibility_sql(include_past=True, force=False)
        self.assertIn("p.date >= CURRENT_DATE", automatic)
        self.assertNotIn("p.date >= CURRENT_DATE", historical)

    def test_explicit_reanalysis_bypasses_existing_analysis_state(self):
        eligibility = analyzer.eligibility_sql(
            include_past=False,
            force=False,
            reanalyze=True,
        )
        self.assertEqual(eligibility, "p.date >= CURRENT_DATE")

    def test_machine_result_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            analyzer.write_result(
                path,
                status="completed",
                selected_count=5,
                source="Example Hall",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["selected_count"], 5)
        self.assertEqual(payload["source_count"], 1)

    def test_page_failures_produce_honest_source_status(self):
        self.assertEqual(analyzer.completion_status(0, 10), "completed")
        self.assertEqual(analyzer.completion_status(2, 10), "partial")
        self.assertEqual(analyzer.completion_status(10, 10), "fatal")

    def test_authentication_errors_are_normalized_before_worker_handoff(self):
        error = analyzer.normalize_codex_auth_error(RuntimeError("Codex is not logged in"))
        self.assertIsInstance(error, analyzer.CodexAuthRequiredError)
        self.assertEqual(error.reason_code, "login_required")

    def test_classical_promotion_uses_shared_crawler_insert_lock(self):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor
        result = self.valid_result()
        with patch.object(
            analyzer,
            "promote_classical_event",
            return_value=("promoted", 11),
        ):
            analyzer.persist_page_result(
                connection,
                run_id=9,
                result=result,
                model="gpt-test",
                promote=True,
            )

        query, params = cursor.execute.call_args_list[0].args
        self.assertIn("pg_advisory_xact_lock", query)
        self.assertEqual(params, (analyzer.CONCERT_INSERT_ADVISORY_LOCK,))

    def test_shadow_classification_never_calls_promotion(self):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with (
            patch.object(analyzer, "matching_concert", return_value=None),
            patch.object(analyzer, "promote_classical_event") as promote,
        ):
            analyzer.persist_page_result(
                connection,
                run_id=9,
                result=self.valid_result(),
                model="gpt-test",
                promote=False,
            )
        promote.assert_not_called()
        self.assertTrue(
            any(
                "added = %s" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    def test_promotion_requires_commit(self):
        with self.assertRaisesRegex(ValueError, "requires committed"):
            analyzer.run(commit=False, promote=True)

    def test_invalid_turn_is_repaired_before_returning(self):
        page = analyzer.candidate_groups([self.event(1), self.event(2)])
        missing = self.valid_result()
        missing["classifications"][0]["event_ids"] = [1]
        thread = MagicMock()
        with patch.object(
            analyzer,
            "run_turn",
            new=AsyncMock(side_effect=[missing, self.valid_result()]),
        ) as run_turn:
            result, repairs = __import__("asyncio").run(
                analyzer.run_validated_turn(
                    thread,
                    "initial",
                    page,
                    "gpt-test",
                    10,
                    source="Example Hall",
                    page_number=1,
                )
            )
        self.assertEqual(result, self.valid_result())
        self.assertEqual(repairs, 1)
        self.assertIn("expected event ID", run_turn.await_args_list[1].args[1])

    def test_codex_turn_allows_read_only_lookup_commands_with_network(self):
        thread = MagicMock()
        turn = MagicMock()
        turn.run = AsyncMock(
            return_value=MagicMock(
                error=None,
                final_response=json.dumps(self.valid_result()),
            )
        )
        thread.turn = AsyncMock(return_value=turn)

        result = __import__("asyncio").run(
            analyzer.run_turn(thread, "prompt", "gpt-test", 10)
        )

        self.assertEqual(result, self.valid_result())
        self.assertEqual(
            thread.turn.await_args.kwargs["sandbox"],
            analyzer.Sandbox.workspace_write,
        )

    def test_codex_client_enables_network_for_database_and_page_lookup(self):
        codex = AsyncMock()
        codex.__aenter__.return_value = codex
        codex.thread_start.return_value = MagicMock(id="thread-1")
        with (
            patch.object(analyzer, "AsyncCodex", return_value=codex) as client,
            patch.object(analyzer, "validate_model", AsyncMock()),
        ):
            __import__("asyncio").run(
                analyzer.analyze_source(
                    [],
                    source="Example Hall",
                    source_url="https://example.test",
                    model="gpt-test",
                    commit=False,
                    conn=None,
                    run_id=None,
                    maximum_events=100,
                    maximum_chars=120_000,
                    timeout_seconds=10,
                    heartbeat_path=None,
                )
            )

        config = client.call_args.args[0]
        self.assertIn(
            "sandbox_workspace_write.network_access=true",
            config.config_overrides,
        )
        self.assertIn('web_search="live"', config.config_overrides)

    def test_codex_thread_uses_ephemeral_environment_setting(self):
        codex = AsyncMock()
        codex.__aenter__.return_value = codex
        codex.thread_start.return_value = MagicMock(id="thread-1")
        with (
            patch.dict(os.environ, {"CODEX_EPHEMERAL": "true"}),
            patch.object(analyzer, "AsyncCodex", return_value=codex),
            patch.object(analyzer, "validate_model", AsyncMock()),
        ):
            __import__("asyncio").run(
                analyzer.analyze_source(
                    [],
                    source="Example Hall",
                    source_url="https://example.test",
                    model="gpt-test",
                    commit=False,
                    conn=None,
                    run_id=None,
                    maximum_events=100,
                    maximum_chars=120_000,
                    timeout_seconds=10,
                    heartbeat_path=None,
                )
            )

        self.assertIs(codex.thread_start.await_args.kwargs["ephemeral"], True)


if __name__ == "__main__":
    unittest.main()
