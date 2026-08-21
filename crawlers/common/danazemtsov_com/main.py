import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Dana Zemtsov"
SOURCE_URL = "https://www.danazemtsov.com/"
FEED_URL = "https://www.danazemtsov.com/concert-archive-source?format=json"
SITE_TIMEZONE = ZoneInfo("Europe/Berlin")

COUNTRY_CODES = {
    "Germany": "DE",
    "Ireland": "IE",
    "Netherlands": "NL",
    "Spain": "ES",
    "United Kingdom": "GB",
}


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _description(item: dict) -> str | None:
    parts = []
    for field in ("excerpt", "body"):
        markup = item.get(field)
        if markup:
            text = BeautifulSoup(markup, "html.parser").get_text("\n", strip=True)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{2,}", "\n", text).strip()
            if text and text not in parts:
                parts.append(text)
    return "\n\n".join(parts) or None


def _date_and_time(timestamp_ms: int) -> tuple[str, str]:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=SITE_TIMEZONE)
    return value.date().isoformat(), value.time().replace(microsecond=0).isoformat()


def _parse_item(item: dict) -> dict | None:
    location = item.get("location") or {}
    venue = _clean_text(location.get("addressTitle"))
    address_line = _clean_text(location.get("addressLine2"))
    city = address_line.split(",", 1)[0].strip()
    country_code = COUNTRY_CODES.get(_clean_text(location.get("addressCountry")))

    # The calendar also contains broad festival spans and masterclasses without
    # locations. A missing venue, city, or country cannot form a valid record.
    if not venue or not city or not country_code or not item.get("startDate"):
        log_message(
            "Skipping calendar item without a complete location",
            event="crawler_item_skipped",
            url=requests.compat.urljoin(SOURCE_URL, item.get("fullUrl") or ""),
        )
        return None

    date, time_from = _date_and_time(item["startDate"])
    time_to = None
    if item.get("endDate"):
        end_date, end_time = _date_and_time(item["endDate"])
        if end_date == date:
            time_to = end_time

    return {
        "title": _clean_text(item.get("title")),
        "date": date,
        "url": requests.compat.urljoin(SOURCE_URL, item["fullUrl"]),
        "time_from": time_from,
        "time_to": time_to,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": _description(item),
    }


class DanaZemtsovCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="danazemtsov_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert feed", event="crawler_url_fetch", url=FEED_URL)
        response = requests.get(
            FEED_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()

        records = []
        for item in payload.get("upcoming", []) + payload.get("past", []):
            record = _parse_item(item)
            if record and record["title"] and record["url"]:
                records.append(record)

        log_message(
            "Concert feed parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
            url=FEED_URL,
        )
        return records


def main():
    DanaZemtsovCrawler().run()


if __name__ == "__main__":
    main()
