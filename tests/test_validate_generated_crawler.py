import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pandas as pd

from automation.validate_generated_crawler import (
    module_name,
    runtime_failure_kind,
    validate_records,
)
from crawlers.base import BaseCrawler, CrawlerConfig


def valid_record(**overrides):
    record = {
        "title": "Example concert",
        "date": "2020-01-02",
        "url": "https://example.test/events/1",
        "time_from": None,
        "venue": "Example Hall",
        "city": "Prague",
        "country_code": "CZ",
        "type": "classical_concert",
        "description": None,
        "source_url": "https://example.test/",
        "source": "Example",
    }
    record.update(overrides)
    return record


class ExampleCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="example_test",
        source="Example",
        source_url="https://example.test/",
        country_code="cz",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "venue",
            "city",
            "country_code",
            "type",
            "description",
            "source_url",
            "source",
        ],
        dedupe_subset=["title", "date", "time_from", "url"],
    )

    def scrape(self):
        return []


class RecordValidationTests(unittest.TestCase):
    def test_module_name_accepts_country_and_common_directories(self):
        self.assertEqual(
            module_name(Path("crawlers/cz/example_cz")),
            "crawlers.cz.example_cz.main",
        )
        self.assertEqual(
            module_name(Path("crawlers/common/example_com")),
            "crawlers.common.example_com.main",
        )

    def test_module_name_rejects_malformed_directories(self):
        for directory in (
            Path("crawlers/europe/example"),
            Path("crawlers/cz"),
            Path("other/cz/example"),
        ):
            with self.subTest(directory=directory):
                with self.assertRaisesRegex(ValueError, "crawler directory must be"):
                    module_name(directory)

    def test_accepts_past_record_with_optional_fields_missing(self):
        self.assertEqual(
            validate_records([valid_record(type=None, time_from=None, description=None)]),
            [],
        )

    def test_rejects_empty_required_values_and_invalid_date(self):
        issues = validate_records(
            [
                valid_record(
                    date="2026-02-30",
                    city=pd.NA,
                    venue=" null ",
                )
            ]
        )
        fields = {issue["field"] for issue in issues}
        self.assertTrue({"date", "city", "venue"}.issubset(fields))

        nat_issues = validate_records([valid_record(date=pd.NaT)])
        self.assertIn("date", {issue["field"] for issue in nat_issues})

    def test_rejects_contaminated_and_placeholder_locations(self):
        issues = validate_records(
            [
                valid_record(city="Hukvaldyvstupné 400 Kč"),
                valid_record(title="Second", url="https://example.test/events/2", city="Brno", venue="Brno"),
            ]
        )
        self.assertEqual(
            [(issue["record"], issue["field"]) for issue in issues],
            [(0, "city"), (1, "venue")],
        )

    def test_rejects_duplicates_after_preparation(self):
        issues = validate_records([valid_record(), valid_record()])
        self.assertEqual(len(issues), 1)
        self.assertIn("duplicate", issues[0]["reason"])

    def test_empty_result_is_invalid(self):
        self.assertEqual(validate_records([])[0]["reason"], "crawler returned no records")

    def test_network_errors_are_inconclusive(self):
        self.assertEqual(runtime_failure_kind(TimeoutError("slow")), "inconclusive_runtime")
        self.assertEqual(runtime_failure_kind(RuntimeError("parser bug")), "execution_error")


class PreparationTests(unittest.TestCase):
    def test_prepare_records_matches_production_transformations_without_side_effects(self):
        crawler = ExampleCrawler()
        duplicate = valid_record(country_code="cz")
        crawler.upload = Mock(side_effect=AssertionError("must not upload"))

        records = crawler.prepare_records([duplicate, dict(duplicate)])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["country_code"], "CZ")
        crawler.upload.assert_not_called()

    def test_prepare_records_converts_pandas_missing_values_to_none(self):
        record = valid_record(
            date=pd.NaT,
            time_from=float("nan"),
            description=pd.NA,
        )

        prepared = ExampleCrawler().prepare_records([record])

        self.assertIsNone(prepared[0]["date"])
        self.assertIsNone(prepared[0]["time_from"])
        self.assertIsNone(prepared[0]["description"])

    def test_config_without_default_country_accepts_and_normalizes_record_countries(self):
        class CommonCrawler(BaseCrawler):
            config = CrawlerConfig(
                slug="common_test",
                source="Common",
                source_url="https://example.test/",
                country_code=None,
            )

        records = CommonCrawler().prepare_records([valid_record(country_code="cz")])

        self.assertEqual(records[0]["country_code"], "CZ")

    def test_config_without_default_country_requires_country_column(self):
        class CommonCrawler(BaseCrawler):
            config = CrawlerConfig(
                slug="common_test",
                source="Common",
                source_url="https://example.test/",
                country_code=None,
            )

        record = valid_record()
        del record["country_code"]

        with self.assertRaisesRegex(
            ValueError,
            "country_code is required on every record",
        ):
            CommonCrawler().prepare_records([record])


if __name__ == "__main__":
    unittest.main()
