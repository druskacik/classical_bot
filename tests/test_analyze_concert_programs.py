import asyncio
import io
import json
import os
import tempfile
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agent_utils import concert_catalog
from agent_utils.concert_catalog import normalize
from analyzers import analyze_concert_programs as analyzer
from automation.codex_auth import CodexAuthRequiredError


def not_needed_location():
    return {
        "status": "not_needed", "existing_city_id": None,
        "english_name": None, "local_name": None, "country_code": None,
        "external_id": None, "raw_value_type": None,
        "source_url": "", "evidence": "",
    }


def no_program_group_result(concerts):
    return {
        "programme_groups": [{
            "concert_ids": [concert.id for concert in concerts],
            "status": "no_program",
            "notes": "No programme published.",
            "composers": [],
            "program": [],
            "unresolved_program": [],
        }],
        "concert_results": [{
            "concert_id": concert.id,
            "source_url": concert.url,
            "event_updates": [],
            "location_resolution": not_needed_location(),
        } for concert in concerts],
    }


class AnalyzeConcertProgramsTests(unittest.TestCase):
    def test_auth_failure_stops_group_without_persisting_concert_error(self):
        concert = analyzer.Concert(
            1, "Test", date.today(), "https://example.test/event", None,
            source="Example", source_url="https://example.test",
        )
        group = analyzer.group_concerts([concert])[0]
        with (
            patch.object(
                analyzer,
                "run_agent",
                new_callable=AsyncMock,
                side_effect=CodexAuthRequiredError("refresh_token_revoked"),
            ),
            patch.object(analyzer, "persist_concert_error") as persist_error,
            self.assertRaises(CodexAuthRequiredError),
        ):
            asyncio.run(
                analyzer.analyze_concert_group(
                    MagicMock(), asyncio.Semaphore(1), group, "model", True, 30
                )
            )
        persist_error.assert_not_called()

    @patch.object(analyzer.psycopg2, "connect")
    def test_database_connections_detect_disappeared_clients_quickly(self, connect):
        with patch.dict(
            os.environ,
            {
                "DB_NAME": "classical",
                "DB_USER": "bot",
                "DB_PASS": "secret",
                "DB_HOST": "postgres",
                "DB_PORT": "5432",
            },
        ):
            analyzer.get_connection()

        connect.assert_called_once_with(
            dbname="classical",
            user="bot",
            password="secret",
            host="postgres",
            port="5432",
            keepalives=1,
            keepalives_idle=60,
            keepalives_interval=20,
            keepalives_count=3,
        )

    def test_normalize_handles_diacritics_and_punctuation(self):
        self.assertEqual(normalize("  Antonín DVOŘÁK — op. 95 "), "antonin dvorak op 95")

    def test_prompt_requires_live_url_before_description(self):
        concert = analyzer.Concert(
            7, "Test", date(2026, 8, 1), "https://example.test/event", "fallback",
            source="Example", source_url="https://example.test",
        )
        prompt = analyzer.render_prompt(analyzer.group_concerts([concert])[0])
        self.assertIn("Always try to open and inspect the live URL first", prompt)
        self.assertIn("fallback context", prompt)
        self.assertIn(
            'python -m agent_utils.concert_catalog find-composer --name "NAME"',
            prompt,
        )
        self.assertIn("python -m agent_utils.concert_catalog list-works --composer-id ID", prompt)
        self.assertNotIn("uv run", prompt)
        self.assertIn("standard English title of the complete composition", prompt)
        self.assertIn("standard English name in programme_label", prompt)
        self.assertIn("commonly established in English-language classical references", prompt)
        self.assertIn("original wording in evidence", prompt)
        self.assertIn("Keep uncertainty local", prompt)
        self.assertIn("Divertimento in D major (selection)", prompt)
        self.assertIn("without a Köchel number", prompt)
        self.assertIn("Your primary task is the composer and work extraction", prompt)
        self.assertIn("Otherwise return an empty event_updates list", prompt)
        self.assertLess(
            prompt.index("Your primary task is the composer and work extraction"),
            prompt.index("As a secondary best-effort task"),
        )
        self.assertIn("Event URL: https://example.test/event", prompt)
        self.assertIn("partition the supplied concert IDs", prompt)

    def test_groups_by_source_normalized_title_and_source_url_without_size_cap(self):
        concerts = [
            analyzer.Concert(
                index,
                "Vivaldi — Four Seasons" if index % 2 else "vivaldi four seasons",
                date.today(),
                f"https://example.test/event/{index}",
                "Same programme",
                source="Example",
                source_url="https://example.test/series",
            )
            for index in range(1, 121)
        ]

        groups = analyzer.group_concerts(concerts)

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].concerts), 120)
        prompt = analyzer.render_prompt(groups[0])
        self.assertEqual(prompt.count("Same programme"), 1)
        self.assertEqual(prompt.count("Concert ID:"), 120)

    def test_grouping_separates_sources_urls_and_missing_keys(self):
        base = dict(
            title="Test!", date=date.today(), description=None,
            source="Example", source_url="https://example.test/series",
        )
        concerts = [
            analyzer.Concert(1, url="https://example.test/1", **base),
            analyzer.Concert(2, url="https://example.test/2", **{**base, "title": "test"}),
            analyzer.Concert(3, url="https://example.test/3", **{**base, "source": "Other"}),
            analyzer.Concert(4, url="https://example.test/4", **{**base, "source_url": "https://example.test/other"}),
            analyzer.Concert(5, url="https://example.test/5", **{**base, "source_url": None}),
            analyzer.Concert(6, url="https://example.test/6", **{**base, "source_url": None}),
        ]

        groups = analyzer.group_concerts(concerts)

        self.assertEqual([len(group.concerts) for group in groups], [2, 1, 1, 1, 1])

    def test_expands_agent_partitions_to_legacy_per_concert_results(self):
        concerts = [
            analyzer.Concert(
                index, "Test", date.today(), f"https://example.test/{index}", None,
                source="Example", source_url="https://example.test/series",
            )
            for index in (1, 2, 3)
        ]
        group = analyzer.group_concerts(concerts)[0]
        result = no_program_group_result(concerts)
        result["programme_groups"] = [
            {
                "concert_ids": [1, 3], "status": "no_program", "notes": "Same",
                "composers": [], "program": [], "unresolved_program": [],
            },
            {
                "concert_ids": [2], "status": "page_unavailable", "notes": "Unavailable",
                "composers": [], "program": [], "unresolved_program": [],
            },
        ]

        expanded = analyzer.expand_group_result(group, result)

        self.assertEqual([concert.id for concert, _ in expanded], [1, 2, 3])
        self.assertEqual([item["status"] for _, item in expanded], ["no_program", "page_unavailable", "no_program"])
        self.assertEqual(expanded[1][1]["source_url"], "https://example.test/2")

    def test_group_result_requires_exact_id_coverage(self):
        concerts = [
            analyzer.Concert(
                index, "Test", date.today(), f"https://example.test/{index}", None,
                source="Example", source_url="https://example.test/series",
            )
            for index in (1, 2)
        ]
        group = analyzer.group_concerts(concerts)[0]

        missing = no_program_group_result(concerts)
        missing["programme_groups"][0]["concert_ids"] = [1]
        with self.assertRaisesRegex(ValueError, "Missing programme results.*2"):
            analyzer.expand_group_result(group, missing)

        duplicate = no_program_group_result(concerts)
        duplicate["programme_groups"].append({**duplicate["programme_groups"][0], "concert_ids": [1]})
        with self.assertRaisesRegex(ValueError, "Duplicate concert ID 1"):
            analyzer.expand_group_result(group, duplicate)

        unknown = no_program_group_result(concerts)
        unknown["concert_results"][1]["concert_id"] = 99
        with self.assertRaisesRegex(ValueError, "Unknown concert ID 99"):
            analyzer.expand_group_result(group, unknown)

    @patch.object(concert_catalog, "get_connection")
    def test_composer_lookup_uses_alias_and_returns_canonical_name_once(self, get_connection):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (5, "Pyotr Ilyich Tchaikovsky", "pyotr ilyich tchaikovsky", "Piotr Iľjič Čajkovskij", "piotr iljic cajkovskij"),
            (5, "Pyotr Ilyich Tchaikovsky", "pyotr ilyich tchaikovsky", "P. I. Čajkovskij", "p i cajkovskij"),
            (16, "Dmitri Shostakovich", "dmitri shostakovich", "Dmitrij Šostakovič", "dmitrij sostakovic"),
        ]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        get_connection.return_value = connection

        result = concert_catalog.find_composers("Piotr Iľjič Čajkovskij")

        self.assertEqual(
            result[0],
            {"id": 5, "name": "Pyotr Ilyich Tchaikovsky", "score": 1.0},
        )
        self.assertEqual([item["id"] for item in result].count(5), 1)
        self.assertIn("LEFT JOIN composer_alias", cursor.execute.call_args.args[0])

    def test_composer_lookup_rejects_empty_input_without_database_query(self):
        with patch.object(concert_catalog, "get_connection") as get_connection:
            self.assertEqual(concert_catalog.find_composers(" -- "), [])
        get_connection.assert_not_called()

    def test_no_program_result_must_not_include_program_entries(self):
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        result = {
            "status": "no_program",
            "source_url": "https://example.test",
            "notes": "unclear",
            "composers": [],
            "program": [{"unexpected": "entry"}],
            "unresolved_program": [],
        }
        with self.assertRaisesRegex(ValueError, "must not contain"):
            analyzer.validate_result(MagicMock(), concert, result)

    def test_ambiguous_result_requires_only_unresolved_entries(self):
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        result = {
            "status": "ambiguous",
            "source_url": "https://example.test",
            "notes": "unclear",
            "composers": [],
            "program": [],
            "unresolved_program": [
                {
                    "programme_label": "Prelude or Toccata",
                    "evidence": "The page lists both alternatives.",
                    "reason": "The performed alternative is not identified.",
                }
            ],
        }
        analyzer.validate_result(MagicMock(), concert, result)

    def test_complete_result_requires_program(self):
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        result = {
            "status": "complete",
            "source_url": "https://example.test",
            "notes": "",
            "composers": [],
            "program": [],
            "unresolved_program": [],
        }
        with self.assertRaisesRegex(ValueError, "at least one"):
            analyzer.validate_result(MagicMock(), concert, result)

    def test_automatic_selection_excludes_past_concerts(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []

        analyzer.select_concerts(conn, concert_ids=None, limit=25, force=False)

        query = cursor.execute.call_args.args[0]
        self.assertIn("c.program_analysis_eligible = true", query)
        self.assertIn("c.source_url", query)
        self.assertIn("c.date >= CURRENT_DATE", query)
        self.assertIn("a.attempts < %s", query)
        self.assertIn("make_interval(days => %s)", query)
        self.assertIn("make_interval(hours => %s)", query)
        self.assertIn("a.status IN ('no_program', 'partial', 'ambiguous')", query)
        self.assertEqual(
            cursor.execute.call_args.args[1],
            (
                analyzer.MAX_AUTOMATIC_ATTEMPTS,
                analyzer.NO_PROGRAM_RETRY_INTERVAL_DAYS,
                analyzer.MAX_AUTOMATIC_ATTEMPTS,
                analyzer.NO_PROGRAM_RETRY_INTERVAL_DAYS,
                analyzer.MAX_AUTOMATIC_ATTEMPTS,
                analyzer.TECHNICAL_RETRY_INTERVAL_HOURS,
                25,
            ),
        )

    def test_automatic_selection_has_no_default_batch_limit(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []

        analyzer.select_concerts(
            conn,
            concert_ids=None,
            limit=analyzer.DEFAULT_LIMIT,
            force=False,
        )

        self.assertIsNone(analyzer.DEFAULT_LIMIT)
        self.assertIsNone(cursor.execute.call_args.args[1][-1])

    def test_composer_only_requires_composers_and_no_program(self):
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        result = {
            "status": "composer_only",
            "source_url": "https://example.test",
            "notes": "Mozart works are not specified.",
            "composers": [],
            "program": [],
            "unresolved_program": [],
        }
        with self.assertRaisesRegex(ValueError, "must contain composers"):
            analyzer.validate_result(MagicMock(), concert, result)

    def test_partial_requires_confident_and_unresolved_entries(self):
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        result = {
            "status": "partial",
            "source_url": concert.url,
            "notes": "",
            "composers": [{"existing_id": 1, "name": "Wolfgang Amadeus Mozart"}],
            "program": [],
            "unresolved_program": [
                {
                    "programme_label": "Divertimento in D major (selection)",
                    "evidence": "The page lists the title without a Köchel number.",
                    "reason": "Multiple Mozart divertimenti match.",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "confident programme entries"):
            analyzer.validate_result(MagicMock(), concert, result)

    @patch.object(analyzer, "_resolve_composer", return_value=17)
    def test_composer_only_replaces_catalogue_links(self, _resolve_composer):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        result = {
            "status": "composer_only",
            "source_url": concert.url,
            "notes": "No works named.",
            "composers": [{"existing_id": 17, "name": "Wolfgang Amadeus Mozart"}],
            "program": [],
            "unresolved_program": [],
        }

        analyzer.persist_result(conn, concert, result, "gpt-5.6-terra")

        executed = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("DELETE FROM classical_concert_work" in query for query in executed))
        self.assertTrue(any("DELETE FROM classical_concert_composer" in query for query in executed))
        self.assertTrue(any("INSERT INTO classical_concert_composer" in query for query in executed))
        upsert = next(query for query in executed if "INSERT INTO concert_program_analysis" in query)
        self.assertIn("EXCLUDED.status = 'no_program'", upsert)
        conn.commit.assert_called_once()

    @patch.object(analyzer, "_resolve_work", return_value=96)
    @patch.object(analyzer, "_resolve_composer", return_value=1)
    def test_partial_saves_confident_work_but_not_unresolved_mozart_divertimento(
        self,
        _resolve_composer,
        resolve_work,
    ):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        concert = analyzer.Concert(2392, "Vivaldi Four Seasons", date.today(), "https://example.test", None)
        composer = {"existing_id": 1, "name": "Wolfgang Amadeus Mozart"}
        result = {
            "status": "partial",
            "source_url": concert.url,
            "notes": "One Mozart slot cannot be identified.",
            "composers": [composer],
            "program": [
                {
                    "composer": composer,
                    "work": {
                        "existing_id": 96,
                        "title": "Exsultate, jubilate",
                        "catalogue_number": "K. 165",
                    },
                    "programme_label": "Alleluia",
                    "evidence": "The page names Exsultate, jubilate.",
                }
            ],
            "unresolved_program": [
                {
                    "programme_label": "Divertimento in D major (selection)",
                    "evidence": "The page gives no Köchel number.",
                    "reason": "Multiple Mozart divertimenti in D major match.",
                }
            ],
        }

        analyzer.persist_result(conn, concert, result, "gpt-5.6-terra")

        resolve_work.assert_called_once()
        self.assertEqual(resolve_work.call_args.args[2]["title"], "Exsultate, jubilate")
        work_inserts = [
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO classical_concert_work" in call.args[0]
        ]
        self.assertEqual(len(work_inserts), 1)
        self.assertEqual(work_inserts[0].args[1][2], "Alleluia")
        analysis_upsert = next(
            call for call in cursor.execute.call_args_list if "INSERT INTO concert_program_analysis" in call.args[0]
        )
        self.assertEqual(analysis_upsert.args[1][1], "partial")

    def test_degraded_retry_preserves_existing_partial_result(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("partial",)
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        result = {
            "status": "ambiguous",
            "source_url": concert.url,
            "notes": "The updated page is unclear.",
            "composers": [],
            "program": [],
            "unresolved_program": [
                {
                    "programme_label": "Unclear selection",
                    "evidence": "The page is unclear.",
                    "reason": "No work is uniquely identified.",
                }
            ],
        }

        analyzer.persist_result(conn, concert, result, "gpt-5.6-terra")

        executed = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any("DELETE FROM classical_concert_work" in query for query in executed))
        self.assertFalse(any("INSERT INTO concert_program_analysis" in query for query in executed))
        update = next(query for query in executed if "UPDATE concert_program_analysis" in query)
        self.assertIn("attempts = attempts + 1", update)
        conn.commit.assert_called_once()

    def test_error_retry_preserves_existing_partial_status(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)

        analyzer.persist_error(conn, concert, "gpt-5.6-terra", RuntimeError("network error"))

        upsert = cursor.execute.call_args.args[0]
        self.assertIn("concert_program_analysis.status = 'partial'", upsert)
        self.assertIn("THEN concert_program_analysis.model", upsert)
        conn.commit.assert_called_once()

    def test_complete_requires_program_composers_in_top_level_list(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        result = {
            "status": "complete",
            "source_url": concert.url,
            "notes": "",
            "composers": [{"existing_id": 1, "name": "Wolfgang Amadeus Mozart"}],
            "unresolved_program": [],
            "program": [
                {
                    "composer": {"existing_id": 2, "name": "Joseph Haydn"},
                    "work": {"existing_id": None, "title": "Symphony No. 1", "catalogue_number": None},
                    "programme_label": "Symphony No. 1",
                    "evidence": "Programme listing",
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "top-level composers"):
            analyzer.validate_result(conn, concert, result)

    @patch.object(analyzer, "_resolve_work", side_effect=[31, 31, 31, 44])
    @patch.object(analyzer, "_resolve_composer", return_value=17)
    def test_complete_aggregates_distinct_excerpts_by_canonical_work(
        self,
        _resolve_composer,
        _resolve_work,
    ):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        composer = {"existing_id": 17, "name": "Max Reger"}

        def entry(work_id, title, label, evidence):
            return {
                "composer": composer,
                "work": {
                    "existing_id": work_id,
                    "title": title,
                    "catalogue_number": None,
                },
                "programme_label": label,
                "evidence": evidence,
            }

        result = {
            "status": "complete",
            "source_url": " https://example.test/programme ",
            "notes": "",
            "composers": [composer],
            "unresolved_program": [],
            "program": [
                entry(31, "Twelve Pieces for Organ", "No. 8: Romance", "Lists No. 8."),
                entry(31, "Twelve Pieces for Organ", "No. 11: Toccata", "Lists No. 11."),
                entry(31, "Twelve Pieces for Organ", "No. 11: Toccata", "Lists No. 11."),
                entry(44, "Ave Maria", "Ave Maria", "Lists Ave Maria."),
            ],
        }

        analyzer.persist_result(conn, concert, result, "gpt-5.6-terra")

        work_inserts = [
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO classical_concert_work" in call.args[0]
        ]
        self.assertEqual(len(work_inserts), 2)
        self.assertEqual(
            work_inserts[0].args[1],
            (
                1,
                31,
                "No. 8: Romance; No. 11: Toccata",
                "https://example.test/programme",
                "Lists No. 8.\nLists No. 11.",
            ),
        )
        self.assertEqual(
            work_inserts[1].args[1],
            (1, 44, "Ave Maria", "https://example.test/programme", "Lists Ave Maria."),
        )
        conn.commit.assert_called_once()

    def test_agent_threads_are_persistent(self):
        codex = MagicMock()
        codex.thread_start = AsyncMock()
        thread = codex.thread_start.return_value
        thread.turn = AsyncMock()
        turn = thread.turn.return_value
        turn.run = AsyncMock()
        turn.run.return_value.error = None
        concert = analyzer.Concert(
            1, "Test", date.today(), "https://example.test/event", None,
            source="Example", source_url="https://example.test",
        )
        group = analyzer.group_concerts([concert])[0]
        turn.run.return_value.final_response = json.dumps(no_program_group_result([concert]))

        asyncio.run(analyzer.run_agent(codex, group, "gpt-5.6-terra", timeout_seconds=30))

        self.assertIs(codex.thread_start.call_args.kwargs["ephemeral"], False)

    def test_dry_run_never_persists(self):
        coordinator_conn = MagicMock()
        worker_conn = MagicMock()
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        result = no_program_group_result([concert])
        codex_class = MagicMock()
        codex_class.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        codex_class.return_value.__aexit__ = AsyncMock(return_value=None)
        with (
            patch.object(analyzer, "AsyncCodex", codex_class),
            patch.object(
                analyzer,
                "get_connection",
                side_effect=[coordinator_conn, worker_conn],
            ),
            patch.object(analyzer, "select_concerts", return_value=[concert]),
            patch.object(analyzer, "validate_model", new_callable=AsyncMock),
            patch.object(analyzer, "run_agent", new_callable=AsyncMock, return_value=result),
            patch.object(analyzer, "validate_result"),
            patch.object(analyzer, "persist_result") as persist_result,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            failures = analyzer.run(concert_ids=[1], commit=False)
        self.assertEqual(failures, 0)
        config = codex_class.call_args.args[0]
        self.assertIsNone(config.codex_bin)
        persist_result.assert_not_called()
        coordinator_conn.commit.assert_not_called()
        worker_conn.commit.assert_not_called()
        coordinator_conn.close.assert_called_once()
        worker_conn.close.assert_called_once()

    def test_committed_batch_refuses_advisory_lock_contention(self):
        conn = MagicMock()
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "batch-result.json"
            with (
                patch.object(analyzer, "get_connection", return_value=conn),
                patch.object(analyzer, "acquire_lock", return_value=False),
                self.assertRaisesRegex(RuntimeError, "already running"),
            ):
                analyzer.run(commit=True, limit=100, result_path=result_path)
            payload = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "fatal")
        conn.close.assert_called_once()

    def test_output_schema_enforces_paired_entities(self):
        programme_group = analyzer.OUTPUT_SCHEMA["properties"]["programme_groups"]["items"]
        self.assertIn("composer_only", programme_group["properties"]["status"]["enum"])
        self.assertIn("partial", programme_group["properties"]["status"]["enum"])
        self.assertIn("concert_ids", programme_group["required"])
        item = programme_group["properties"]["program"]["items"]
        self.assertEqual(
            item["required"],
            ["composer", "work", "programme_label", "evidence"],
        )
        unresolved = programme_group["properties"]["unresolved_program"]["items"]
        self.assertEqual(
            unresolved["required"],
            ["programme_label", "evidence", "reason"],
        )
        concert_result = analyzer.OUTPUT_SCHEMA["properties"]["concert_results"]["items"]
        event_update = concert_result["properties"]["event_updates"]["items"]
        self.assertEqual(
            event_update["required"],
            ["field", "new_value", "source_url", "evidence"],
        )
        self.assertIn("location_resolution", concert_result["required"])
        location = concert_result["properties"]["location_resolution"]
        self.assertNotIn("external_source", location["properties"])
        self.assertNotIn("external_source", location["required"])

    def test_validates_supported_event_updates_and_rejects_location_fields(self):
        conn = MagicMock()
        concert = analyzer.Concert(
            1,
            "Test",
            date(2026, 8, 1),
            "https://example.test",
            None,
            time(19, 0),
            None,
            "Praha",
            "CZ",
            "Old Hall",
            "scheduled",
        )
        updates = [
            {
                "field": "time_from",
                "new_value": "20:00",
                "source_url": concert.url,
                "evidence": "The event page gives 20:00.",
            },
            {
                "field": "city",
                "new_value": "Praha",
                "source_url": concert.url,
                "evidence": "The event page says Praha.",
            },
        ]

        accepted = analyzer.validate_event_updates(conn, concert, updates)

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["field"], "time_from")
        self.assertEqual(accepted[0]["db_value"], time(20, 0))

    def test_validates_existing_city_and_rejects_country_conflict(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("Prague", "Praha", "CZ")
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        proposal = {
            "status": "existing_city", "existing_city_id": 7,
            "english_name": None, "local_name": None, "country_code": "CZ",
            "external_id": None,
            "raw_value_type": "legitimate_name",
            "source_url": "https://example.test", "evidence": "The page says Praha.",
        }
        accepted = analyzer.validate_location_resolution(conn, concert, proposal)
        self.assertEqual(accepted["city_id"], 7)
        proposal["country_code"] = "SK"
        self.assertIsNone(analyzer.validate_location_resolution(conn, concert, proposal))

    def test_new_city_requires_stable_external_identity(self):
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        proposal = {
            "status": "new_city", "existing_city_id": None,
            "english_name": "Hukvaldy", "local_name": "Hukvaldy", "country_code": "CZ",
            "external_id": None,
            "raw_value_type": "extraction_artifact",
            "source_url": "https://example.test", "evidence": "The venue is in Hukvaldy.",
        }
        self.assertIsNone(analyzer.validate_location_resolution(MagicMock(), concert, proposal))

    def test_new_city_requires_matching_geonames_url_and_reuses_existing_id(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (12, "CZ")
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        proposal = {
            "status": "new_city", "existing_city_id": None,
            "english_name": "Hukvaldy", "local_name": "Hukvaldy",
            "country_code": "CZ", "external_id": "3074729",
            "raw_value_type": "legitimate_name",
            "source_url": "https://www.geonames.org/3074729/hukvaldy.html",
            "evidence": "The event page identifies Hukvaldy.",
        }

        accepted = analyzer.validate_location_resolution(conn, concert, proposal)

        self.assertEqual(accepted["status"], "existing_city")
        self.assertEqual(accepted["city_id"], 12)
        self.assertEqual(cursor.execute.call_args.args[1], ("3074729",))

        proposal["source_url"] = "https://www.geonames.org/999/wrong.html"
        self.assertIsNone(analyzer.validate_location_resolution(conn, concert, proposal))

    def test_new_city_assigns_server_owned_geonames_source(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        proposal = {
            "status": "new_city", "existing_city_id": None,
            "english_name": "Hukvaldy", "local_name": "Hukvaldy",
            "country_code": "CZ", "external_id": "3074729",
            "raw_value_type": "legitimate_name",
            "source_url": "https://www.geonames.org/3074729/hukvaldy.html",
            "evidence": "The event page identifies Hukvaldy.",
        }

        accepted = analyzer.validate_location_resolution(conn, concert, proposal)

        self.assertEqual(accepted["external_source"], "geonames")

    def test_new_city_insert_uses_geonames_id_conflict_target(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (12, "CZ")
        concert = analyzer.Concert(
            1, "Test", date.today(), "https://example.test", None,
            city_raw="Hukvaldy", country_code_raw="CZ",
        )
        resolution = {
            "status": "new_city", "english_name": "Hukvaldy",
            "local_name": "Hukvaldy", "country_code": "CZ",
            "external_id": "3074729", "raw_value_type": "legitimate_name",
            "source_url": "https://www.geonames.org/3074729/hukvaldy.html",
            "evidence": "The event page identifies Hukvaldy.",
        }

        analyzer.apply_location_resolution(cursor, concert, resolution, "test-model")

        insert_call = next(
            call for call in cursor.execute.call_args_list
            if "INSERT INTO city" in call.args[0]
        )
        self.assertIn("ON CONFLICT (external_id)", insert_call.args[0])
        self.assertEqual(insert_call.args[1][3], "geonames")

    def test_page_unavailable_cannot_resolve_location(self):
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        proposal = {
            "status": "country_only", "country_code": "CZ",
            "source_url": "https://example.test", "evidence": "Stored fallback.",
        }
        self.assertIsNone(
            analyzer.validate_location_resolution(
                MagicMock(), concert, proposal, page_available=False
            )
        )

    def test_persists_existing_city_alias_and_audit(self):
        cursor = MagicMock()
        concert = analyzer.Concert(
            1, "Test", date.today(), "https://example.test", None,
            city_raw="Praha", country_code_raw="SK", source="Test source",
        )
        resolution = {
            "status": "existing_city", "city_id": 7, "country_code": "CZ",
            "raw_value_type": "legitimate_name", "source_url": concert.url,
            "evidence": "The page identifies Prague.",
        }
        analyzer.apply_location_resolution(cursor, concert, resolution, "test-model")
        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("INSERT INTO city_alias" in query for query in queries))
        self.assertTrue(any("country_code_resolved" in query for query in queries))
        self.assertEqual(
            sum("INSERT INTO classical_concert_change" in query for query in queries),
            2,
        )

    def test_skips_alias_for_empty_city_sentinel(self):
        cursor = MagicMock()
        concert = analyzer.Concert(
            1, "Test", date.today(), "https://example.test", None,
            city_raw="NaN", country_code_raw="CZ", source="Test source",
        )
        resolution = {
            "status": "existing_city", "city_id": 7, "country_code": "IT",
            "raw_value_type": "extraction_artifact", "source_url": concert.url,
            "evidence": "The page identifies Sterzing.",
        }

        analyzer.apply_location_resolution(cursor, concert, resolution, "test-model")

        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any("INSERT INTO city_alias" in query for query in queries))
        self.assertTrue(any("UPDATE classical_concert" in query for query in queries))

    def test_rejects_date_update_that_would_duplicate_a_concert(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        concert = analyzer.Concert(
            1,
            "Test",
            date(2026, 8, 1),
            "https://example.test",
            None,
        )

        accepted = analyzer.validate_event_updates(
            conn,
            concert,
            [
                {
                    "field": "date",
                    "new_value": "2026-08-02",
                    "source_url": concert.url,
                    "evidence": "The event page gives 2 August.",
                }
            ],
        )

        self.assertEqual(accepted, [])

    def test_persists_event_update_with_audit_and_verification_timestamp(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        concert = analyzer.Concert(
            1,
            "Test",
            date(2026, 8, 1),
            "https://example.test",
            None,
        )
        result = {
            "status": "no_program",
            "source_url": concert.url,
            "notes": "No programme published.",
            "composers": [],
            "program": [],
            "unresolved_program": [],
            "event_updates": [],
        }
        event_updates = [
            {
                "field": "event_status",
                "db_value": "cancelled",
                "new_value": "cancelled",
                "old_value": "scheduled",
                "source_url": concert.url,
                "evidence": "The organizer marks the event as cancelled.",
            }
        ]

        analyzer.persist_result(
            conn,
            concert,
            result,
            "gpt-5.6-terra",
            event_updates,
        )

        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("last_verified_at = now()" in query for query in queries))
        status_update = next(query for query in queries if "event_status = %s" in query)
        self.assertIn("event_status_updated_at = now()", status_update)
        self.assertTrue(any("INSERT INTO classical_concert_change" in query for query in queries))
        conn.commit.assert_called_once()

    def test_page_unavailable_does_not_mark_concert_verified(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        concert = analyzer.Concert(
            1,
            "Test",
            date(2026, 8, 1),
            "https://example.test",
            None,
        )
        result = {
            "status": "page_unavailable",
            "source_url": concert.url,
            "notes": "The page could not be opened.",
            "composers": [],
            "program": [],
            "unresolved_program": [],
            "event_updates": [],
        }

        analyzer.persist_result(conn, concert, result, "gpt-5.6-terra")

        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any("last_verified_at = now()" in query for query in queries))

    def test_invalid_event_update_does_not_block_programme_result(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        concert = analyzer.Concert(
            1,
            "Test",
            date(2026, 8, 1),
            "https://example.test",
            None,
        )
        result = {
            "status": "no_program",
            "source_url": concert.url,
            "notes": "No programme published.",
            "composers": [],
            "program": [],
            "unresolved_program": [],
            "event_updates": [
                {
                    "field": "time_from",
                    "new_value": "eight o'clock",
                    "source_url": concert.url,
                    "evidence": "Unparseable time.",
                }
            ],
        }

        analyzer.persist_result(conn, concert, result, "gpt-5.6-terra")

        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("INSERT INTO concert_program_analysis" in query for query in queries))
        self.assertFalse(any("INSERT INTO classical_concert_change" in query for query in queries))
        conn.commit.assert_called_once()

    def test_degraded_programme_retry_still_applies_event_update(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("partial",)
        concert = analyzer.Concert(
            1,
            "Test",
            date(2026, 8, 1),
            "https://example.test",
            None,
        )
        result = {
            "status": "no_program",
            "source_url": concert.url,
            "notes": "Programme was removed from the page.",
            "composers": [],
            "program": [],
            "unresolved_program": [],
            "event_updates": [],
        }
        event_updates = [
            {
                "field": "event_status",
                "db_value": "cancelled",
                "new_value": "cancelled",
                "old_value": "scheduled",
                "source_url": concert.url,
                "evidence": "The event is cancelled.",
            }
        ]

        analyzer.persist_result(
            conn,
            concert,
            result,
            "gpt-5.6-terra",
            event_updates,
        )

        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("INSERT INTO classical_concert_change" in query for query in queries))
        self.assertTrue(any("UPDATE concert_program_analysis" in query for query in queries))
        self.assertFalse(any("DELETE FROM classical_concert_work" in query for query in queries))

    def test_model_validation_falls_back_to_local_catalogue(self):
        codex = MagicMock()
        codex.models = AsyncMock(side_effect=ValueError("new enum value"))
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "models_cache.json"), "w", encoding="utf-8") as handle:
                json.dump({"models": [{"slug": "gpt-5.6-terra"}]}, handle)
            with patch.dict(os.environ, {"CODEX_HOME": directory}):
                asyncio.run(analyzer.validate_model(codex, "gpt-5.6-terra"))

    def test_concurrency_defaults_to_four_and_honors_cli_then_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(analyzer.resolve_concurrency(), 4)
        with patch.dict(os.environ, {"CONCERT_PROGRAM_CONCURRENCY": "7"}):
            self.assertEqual(analyzer.resolve_concurrency(), 7)
            self.assertEqual(analyzer.resolve_concurrency(2), 2)

    def test_default_group_timeout_is_thirty_minutes(self):
        self.assertEqual(analyzer.DEFAULT_TIMEOUT_SECONDS, 1800)

    def test_batch_result_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "batch-result.json"
            analyzer.write_batch_result(
                result_path,
                status="completed",
                selected_count=100,
                group_count=60,
                failure_count=2,
            )
            payload = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["selected_count"], 100)
        self.assertEqual(payload["completed_count"], 98)
        self.assertEqual(payload["failure_count"], 2)

    def test_cli_defaults_to_all_eligible_concerts(self):
        with patch("sys.argv", ["analyze_concert_programs"]):
            args = analyzer.parse_args()

        self.assertIsNone(args.limit)
        self.assertIsNone(args.concurrency)

    def test_concurrency_rejects_invalid_values(self):
        for value in (0, -1, "many"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be"):
                    analyzer.resolve_concurrency(value)

    def test_async_batch_never_exceeds_configured_concurrency(self):
        concerts = [
            analyzer.Concert(
                index, f"Test {index}", date.today(), f"https://example.test/{index}", None,
                source="Example", source_url=f"https://example.test/source/{index}",
            )
            for index in range(1, 9)
        ]
        groups = analyzer.group_concerts(concerts)
        active = 0
        maximum_active = 0

        async def agent_result(_codex, group, _model, _timeout):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return no_program_group_result(group.concerts)

        codex_class = MagicMock()
        codex_class.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        codex_class.return_value.__aexit__ = AsyncMock(return_value=None)
        with (
            patch.object(analyzer, "AsyncCodex", codex_class),
            patch.object(analyzer, "validate_model", new_callable=AsyncMock),
            patch.object(
                analyzer,
                "run_agent",
                new=AsyncMock(side_effect=agent_result),
            ) as run_agent,
            patch.object(analyzer, "validate_and_persist_result"),
        ):
            failures = asyncio.run(
                analyzer.run_concert_groups(
                    groups,
                    model="gpt-5.6-terra",
                    commit=False,
                    timeout_seconds=30,
                    concurrency=4,
                )
            )

        self.assertEqual(failures, 0)
        self.assertEqual(run_agent.await_count, 8)
        self.assertEqual(maximum_active, 4)

    def test_parallel_results_use_independent_database_connections(self):
        concerts = [
            analyzer.Concert(
                index, f"Test {index}", date.today(), f"https://example.test/{index}", None,
                source="Example", source_url=f"https://example.test/source/{index}",
            )
            for index in (1, 2)
        ]
        groups = analyzer.group_concerts(concerts)
        connections = [MagicMock(), MagicMock()]

        async def agent_result(_codex, group, _model, _timeout):
            return no_program_group_result(group.concerts)

        with (
            patch.object(analyzer, "run_agent", new=AsyncMock(side_effect=agent_result)),
            patch.object(analyzer, "get_connection", side_effect=connections),
            patch.object(analyzer, "validate_result"),
            patch.object(analyzer, "validate_event_updates", return_value=[]),
            patch.object(analyzer, "validate_location_resolution", return_value=None),
            patch.object(analyzer, "persist_result") as persist_result,
        ):
            async def run_all():
                semaphore = asyncio.Semaphore(2)
                return await asyncio.gather(
                    *(
                        analyzer.analyze_concert_group(
                            MagicMock(),
                            semaphore,
                            group,
                            "gpt-5.6-terra",
                            True,
                            30,
                        )
                        for group in groups
                    )
                )

            results = asyncio.run(run_all())

        self.assertEqual(results, [0, 0])
        self.assertEqual([call.args[0] for call in persist_result.call_args_list], connections)
        for connection in connections:
            connection.close.assert_called_once()

    def test_timeout_interrupts_only_its_turn(self):
        codex = MagicMock()
        codex.thread_start = AsyncMock()
        thread = codex.thread_start.return_value
        thread.turn = AsyncMock()
        turn = thread.turn.return_value

        async def never_finishes():
            await asyncio.Event().wait()

        turn.run = AsyncMock(side_effect=never_finishes)
        turn.interrupt = AsyncMock()
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        group = analyzer.ConcertGroup(("Example", "test", "https://example.test"), (concert,))

        with self.assertRaisesRegex(TimeoutError, "exceeded"):
            asyncio.run(analyzer.run_agent(codex, group, "gpt-5.6-terra", 0.01))
        turn.interrupt.assert_awaited_once()

    def test_failed_timeout_interrupt_marks_codex_client_unhealthy(self):
        codex = MagicMock()
        codex.thread_start = AsyncMock()
        thread = codex.thread_start.return_value
        thread.turn = AsyncMock()
        turn = thread.turn.return_value

        async def never_finishes():
            await asyncio.Event().wait()

        turn.run = AsyncMock(side_effect=never_finishes)
        turn.interrupt = AsyncMock(side_effect=RuntimeError("transport unavailable"))
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        group = analyzer.ConcertGroup(("Example", "test", "https://example.test"), (concert,))

        with self.assertRaises(analyzer.CodexClientUnhealthyError):
            asyncio.run(analyzer.run_agent(codex, group, "gpt-5.6-terra", 0.01))

    def test_timeout_interrupt_is_itself_bounded(self):
        codex = MagicMock()
        codex.thread_start = AsyncMock()
        thread = codex.thread_start.return_value
        thread.turn = AsyncMock()
        turn = thread.turn.return_value

        async def never_finishes():
            await asyncio.Event().wait()

        turn.run = AsyncMock(side_effect=never_finishes)
        turn.interrupt = AsyncMock(side_effect=never_finishes)
        concert = analyzer.Concert(1, "Test", date.today(), "https://example.test", None)
        group = analyzer.ConcertGroup(("Example", "test", "https://example.test"), (concert,))

        with (
            patch.object(analyzer, "INTERRUPT_TIMEOUT_SECONDS", 0.01),
            self.assertRaises(analyzer.CodexClientUnhealthyError),
        ):
            asyncio.run(analyzer.run_agent(codex, group, "gpt-5.6-terra", 0.01))

    def test_batch_heartbeat_is_written_on_start_and_group_terminal_state(self):
        concert = analyzer.Concert(
            1, "Test", date.today(), "https://example.test", None,
            source="Example", source_url="https://example.test",
        )
        groups = analyzer.group_concerts([concert])
        codex_class = MagicMock()
        codex_class.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        codex_class.return_value.__aexit__ = AsyncMock(return_value=None)

        with tempfile.TemporaryDirectory() as temporary:
            heartbeat = Path(temporary) / "heartbeat"
            with (
                patch.object(analyzer, "AsyncCodex", codex_class),
                patch.object(analyzer, "validate_model", new_callable=AsyncMock),
                patch.object(
                    analyzer,
                    "run_agent",
                    new=AsyncMock(return_value=no_program_group_result([concert])),
                ),
                patch.object(analyzer, "validate_and_persist_result"),
                patch.object(Path, "touch", autospec=True) as touch,
            ):
                failures = asyncio.run(
                    analyzer.run_concert_groups(
                        groups,
                        model="gpt-5.6-terra",
                        commit=False,
                        timeout_seconds=30,
                        concurrency=1,
                        heartbeat_path=heartbeat,
                    )
                )

        self.assertEqual(failures, 0)
        self.assertEqual(touch.call_count, 2)

    def test_one_concert_failure_does_not_cancel_siblings(self):
        concerts = [
            analyzer.Concert(
                index, f"Test {index}", date.today(), f"https://example.test/{index}", None,
                source="Example", source_url=f"https://example.test/source/{index}",
            )
            for index in range(1, 4)
        ]
        groups = analyzer.group_concerts(concerts)
        completed = []

        async def agent_result(_codex, group, _model, _timeout):
            concert = group.concerts[0]
            if concert.id == 2:
                raise RuntimeError("one failure")
            completed.append(concert.id)
            return no_program_group_result(group.concerts)

        with (
            patch.object(analyzer, "run_agent", new=AsyncMock(side_effect=agent_result)),
            patch.object(analyzer, "validate_and_persist_result"),
            patch.object(
                analyzer,
                "persist_concert_error",
                side_effect=RuntimeError("error persistence failed"),
            ) as persist_concert_error,
        ):
            async def run_all():
                semaphore = asyncio.Semaphore(2)
                return await asyncio.gather(
                    *(
                        analyzer.analyze_concert_group(
                            MagicMock(),
                            semaphore,
                            group,
                            "gpt-5.6-terra",
                            True,
                            30,
                        )
                        for group in groups
                    )
                )

            results = asyncio.run(run_all())

        self.assertEqual(results, [0, 1, 0])
        self.assertEqual(completed, [1, 3])
        persist_concert_error.assert_called_once()

    def test_one_invalid_concert_result_does_not_block_group_siblings(self):
        concerts = [
            analyzer.Concert(
                index, "Shared", date.today(), f"https://example.test/{index}", None,
                source="Example", source_url="https://example.test/series",
            )
            for index in (1, 2, 3)
        ]
        group = analyzer.group_concerts(concerts)[0]

        def validate(concert, _result, _model, _commit):
            if concert.id == 2:
                raise ValueError("invalid occurrence")

        with (
            patch.object(
                analyzer,
                "run_agent",
                new=AsyncMock(return_value=no_program_group_result(concerts)),
            ),
            patch.object(analyzer, "validate_and_persist_result", side_effect=validate) as persist,
            patch.object(analyzer, "persist_concert_error") as persist_error,
        ):
            failures = asyncio.run(
                analyzer.analyze_concert_group(
                    MagicMock(), asyncio.Semaphore(1), group,
                    "gpt-5.6-terra", True, 30,
                )
            )

        self.assertEqual(failures, 1)
        self.assertEqual([call.args[0].id for call in persist.call_args_list], [1, 2, 3])
        self.assertEqual(persist_error.call_args.args[0].id, 2)


if __name__ == "__main__":
    unittest.main()
