import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "SF Civic Music"
SOURCE_URL = "https://www.sfcivicmusic.org/"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar")
TIME_ZONE = ZoneInfo("America/Los_Angeles")
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
}


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", unescape(str(value)).replace("\xa0", " ")).strip()


def html_to_text(value):
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    text = soup.get_text("\n", strip=True).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def city_from_location(location):
    address_line = clean_text(location.get("addressLine2"))
    if address_line:
        city = clean_text(address_line.split(",", 1)[0])
        if city:
            return city

    # This is the calendar of a San Francisco-based organization and all of
    # its published event locations are local. Use the home city only when a
    # venue is present but Squarespace's optional address line is missing.
    return "San Francisco" if clean_text(location.get("addressTitle")) else None


def parse_item(item):
    title = clean_text(item.get("title"))
    path = clean_text(item.get("fullUrl"))
    location = item.get("location") or {}
    venue = clean_text(location.get("addressTitle"))
    city = city_from_location(location)
    start_timestamp = item.get("startDate")

    if not all((title, path, venue, city, start_timestamp)):
        return None

    try:
        starts_at = datetime.fromtimestamp(start_timestamp / 1000, tz=TIME_ZONE)
    except (OSError, OverflowError, TypeError, ValueError):
        return None

    return {
        "title": title,
        "date": starts_at.date().isoformat(),
        "url": urljoin(SOURCE_URL, path),
        "time_from": starts_at.strftime("%H:%M"),
        "venue": venue,
        "city": city,
        "country_code": "US",
        "description": html_to_text(item.get("body")),
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    offset = None
    seen_offsets = set()
    skipped = 0

    while True:
        params = {"format": "json"}
        if offset is not None:
            params["offset"] = offset

        log_message("Fetching calendar page", event="crawler_url_fetch", url=CALENDAR_URL)
        response = session.get(CALENDAR_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in [*(payload.get("upcoming") or []), *(payload.get("past") or [])]:
            record = parse_item(item)
            if record:
                records.append(record)
            else:
                skipped += 1

        pagination = payload.get("pagination") or {}
        next_offset = pagination.get("nextPageOffset") if pagination.get("nextPage") else None
        if next_offset is None:
            break
        if next_offset in seen_offsets:
            log_message(
                "Stopped calendar pagination after repeated offset",
                event="crawler_pagination_stopped",
                level="warning",
                url=CALENDAR_URL,
                error_type="RepeatedOffset",
                error_message=str(next_offset),
            )
            break
        seen_offsets.add(next_offset)
        offset = next_offset

    if skipped:
        log_message(
            "Skipped events missing required fields",
            event="crawler_records_skipped",
            level="warning",
            url=CALENDAR_URL,
            record_count=skipped,
        )

    unique = {
        (record["url"], record["date"], record["time_from"], record["venue"]): record
        for record in records
    }
    result = sorted(
        unique.values(),
        key=lambda record: (record["date"], record["time_from"] or "", record["title"]),
    )
    log_message(
        "Calendar parsed",
        event="crawler_scrape_completed",
        url=CALENDAR_URL,
        record_count=len(result),
    )
    return result


class SfCivicMusicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sfcivicmusic_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "venue",
            "city",
            "country_code",
            "description",
            "source_url",
            "source",
        ],
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self):
        return scrape_events()


def main():
    SfCivicMusicCrawler().run()


if __name__ == "__main__":
    main()
