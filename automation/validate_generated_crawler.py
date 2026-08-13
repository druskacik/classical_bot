from __future__ import annotations

import argparse
import importlib
import inspect
import io
import json
import math
import re
import socket
import time
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from crawlers.base import BaseCrawler
from crawlers.cities import clean_city_raw, normalize_city_key


EMPTY_VALUES = {"", "-", "n/a", "na", "nan", "none", "null"}
LOCATION_CONTAMINATION = re.compile(
    r"vstupn[eé]|zdarma|\b(?:czk|eur)\b|[€$]|\b\d+(?:[.,]\d+)?\s*kč\b",
    re.IGNORECASE,
)
MAX_EXAMPLES = 10


def module_name(crawler_directory: Path) -> str:
    if (
        crawler_directory.is_absolute()
        or len(crawler_directory.parts) != 3
        or crawler_directory.parts[0] != "crawlers"
        or (
            len(crawler_directory.parts[1]) != 2
            and crawler_directory.parts[1] != "common"
        )
    ):
        raise ValueError(
            "crawler directory must be crawlers/<country>/<slug> or crawlers/common/<slug>"
        )
    return ".".join((*crawler_directory.parts, "main"))


def crawler_class(module: Any) -> type[BaseCrawler]:
    classes = [
        value
        for value in vars(module).values()
        if inspect.isclass(value)
        and issubclass(value, BaseCrawler)
        and value is not BaseCrawler
        and value.__module__ == module.__name__
    ]
    if len(classes) != 1:
        raise ValueError(f"expected exactly one BaseCrawler subclass, found {len(classes)}")
    return classes[0]


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        pandas_missing = pd.isna(value)
    except (TypeError, ValueError):
        pandas_missing = False
    if isinstance(pandas_missing, bool) and pandas_missing:
        return True
    return isinstance(value, str) and value.strip().casefold() in EMPTY_VALUES


def valid_date(value: Any) -> bool:
    if is_missing(value):
        return False
    if isinstance(value, datetime):
        return True
    if isinstance(value, date):
        return True
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return False
    try:
        date.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def valid_http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def add_issue(
    issues: list[dict[str, Any]],
    index: int,
    field: str,
    reason: str,
    value: Any,
) -> None:
    issues.append(
        {
            "record": index,
            "field": field,
            "reason": reason,
            "value": None if value is None else str(value)[:160],
        }
    )


def validate_records(records: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(records, list):
        return [
            {
                "record": None,
                "field": "records",
                "reason": "scrape must return a list",
                "value": type(records).__name__,
            }
        ]
    if not records:
        return [
            {
                "record": None,
                "field": "records",
                "reason": "crawler returned no records",
                "value": "0",
            }
        ]

    identities: dict[tuple[str, str, str, str], int] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            add_issue(issues, index, "record", "record must be a dictionary", type(record).__name__)
            continue

        for field in ("title", "source"):
            value = record.get(field)
            if is_missing(value) or not isinstance(value, str):
                add_issue(issues, index, field, "required nonempty string", value)

        if not valid_date(record.get("date")):
            add_issue(issues, index, "date", "required YYYY-MM-DD date", record.get("date"))

        for field in ("url", "source_url"):
            if not valid_http_url(record.get(field)):
                add_issue(issues, index, field, "required absolute HTTP(S) URL", record.get(field))

        country = record.get("country_code")
        if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
            add_issue(issues, index, "country_code", "required uppercase ISO alpha-2 code", country)

        city_value = record.get("city")
        city = None if is_missing(city_value) else clean_city_raw(city_value)
        venue_value = record.get("venue")
        venue = None if is_missing(venue_value) else str(venue_value).strip()
        if city is None or not isinstance(city_value, str):
            add_issue(issues, index, "city", "required nonempty city", record.get("city"))
        elif LOCATION_CONTAMINATION.search(city):
            add_issue(issues, index, "city", "looks contaminated by ticket or price text", city)
        if venue is None or not isinstance(venue_value, str):
            add_issue(issues, index, "venue", "required nonempty venue", venue_value)
        elif LOCATION_CONTAMINATION.search(venue):
            add_issue(issues, index, "venue", "looks contaminated by ticket or price text", venue)
        if city and venue and normalize_city_key(city) == normalize_city_key(venue):
            add_issue(issues, index, "venue", "venue must not be a city placeholder", venue)

        identity = tuple(
            str(record.get(field) or "").strip()
            for field in ("title", "date", "time_from", "url")
        )
        if identity in identities:
            add_issue(
                issues,
                index,
                "record",
                f"duplicate of record {identities[identity]}",
                identity,
            )
        else:
            identities[identity] = index
    return issues


def runtime_failure_kind(error: BaseException) -> str:
    if isinstance(error, (TimeoutError, socket.gaierror, urllib.error.URLError)):
        return "inconclusive_runtime"
    name = f"{type(error).__module__}.{type(error).__name__}".casefold()
    message = str(error).casefold()
    network_markers = (
        "connectionerror",
        "httperror",
        "requestexception",
        "timeout",
        "timed out",
        "name resolution",
        "name or service not known",
        "temporary failure in name resolution",
        "dns",
    )
    return (
        "inconclusive_runtime"
        if any(marker in name or marker in message for marker in network_markers)
        else "execution_error"
    )


def validate_crawler(crawler_directory: Path) -> dict[str, Any]:
    started = time.monotonic()
    captured = io.StringIO()
    try:
        with redirect_stdout(captured), redirect_stderr(captured):
            module = importlib.import_module(module_name(crawler_directory))
            crawler = crawler_class(module)()
            scraped = crawler.scrape()
            prepared = crawler.prepare_records(scraped)
        issues = validate_records(prepared)
        return {
            "status": "data_quality_failure" if issues else "passed",
            "record_count": len(prepared) if isinstance(prepared, list) else None,
            "issue_count": len(issues),
            "issues": issues[:MAX_EXAMPLES],
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    except Exception as error:
        return {
            "status": runtime_failure_kind(error),
            "record_count": None,
            "issue_count": None,
            "issues": [],
            "error": f"{type(error).__name__}: {error}"[:1000],
            "duration_seconds": round(time.monotonic() - started, 2),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live-validate one generated crawler.")
    parser.add_argument("--crawler-directory", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    report = validate_crawler(parse_args().crawler_directory)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
