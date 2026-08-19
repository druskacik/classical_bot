import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Northwest Symphony Orchestra"
SOURCE_URL = "https://www.northwestsymphonyorchestra.org/"
EVENTS_URL = urljoin(SOURCE_URL, "new-events")
TIME_ZONE = ZoneInfo("America/Los_Angeles")
TIMEOUT = 45
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_text(value) -> str:
    if not value:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text("\n", strip=True)
    text = html.unescape(text).replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def event_datetime(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None


def city_from_location(location: dict) -> str:
    address = clean_text(location.get("addressLine2"))
    if not address:
        return ""
    return address.split(",", 1)[0].strip()


def parse_event(item: dict):
    start = event_datetime(item.get("startDate"))
    end = event_datetime(item.get("endDate"))
    location = item.get("location") or {}
    title = clean_text(item.get("title"))
    venue = clean_text(location.get("addressTitle"))
    city = city_from_location(location)
    path = clean_text(item.get("fullUrl"))
    url = urljoin(SOURCE_URL, path)

    if not title or not start or not path or not venue or not city:
        log_message(
            "Skipping event with incomplete required fields",
            event="crawler_event_skipped",
            level="warning",
            url=url or EVENTS_URL,
            has_title=bool(title),
            has_date=bool(start),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": start.strftime("%H:%M"),
        "time_to": end.strftime("%H:%M") if end else None,
        "venue": venue,
        "city": city,
        "country_code": "US",
        "description": clean_text(item.get("body")) or None,
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


def fetch_events(session: requests.Session) -> list[dict]:
    items = []
    offset = None
    seen_offsets = set()

    while True:
        params = {"format": "json"}
        if offset is not None:
            params["offset"] = offset
        response = session.get(EVENTS_URL, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        page_items = [*(payload.get("upcoming") or []), *(payload.get("past") or [])]
        items.extend(page_items)

        pagination = payload.get("pagination") or {}
        next_offset = pagination.get("nextPageOffset")
        if not pagination.get("nextPage") or not next_offset or next_offset in seen_offsets:
            break
        seen_offsets.add(next_offset)
        offset = next_offset

    return items


class NorthwestSymphonyOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="northwestsymphonyorchestra_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "time_to",
            "venue",
            "city",
            "country_code",
            "description",
            "source_url",
            "source",
        ],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for item in fetch_events(session):
            record = parse_event(item)
            if record:
                records.append(record)

        log_message(
            "Northwest Symphony Orchestra events parsed",
            event="crawler_parse_completed",
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda record: (record["date"], record["time_from"] or "", record["title"]),
        )


def main():
    NorthwestSymphonyOrchestraOrgCrawler().run()


if __name__ == "__main__":
    main()
