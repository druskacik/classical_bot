from datetime import datetime
from html import unescape
import re
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://saltbaychamberfest.org/"
SOURCE = "Salt Bay Chamberfest"
COLLECTION_URL = urljoin(SOURCE_URL, "sbc-concerts")
LOCAL_TIMEZONE = ZoneInfo("America/New_York")


def _text_from_html(value):
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", unescape(text)).strip()
    return text or None


def _city_from_location(location):
    address_line = (location.get("addressLine2") or "").strip()
    if not address_line:
        return None
    city = address_line.split(",", 1)[0].strip()
    return city or None


def _event_to_record(event):
    location = event.get("location") or {}
    venue = _text_from_html(location.get("addressTitle"))
    city = _city_from_location(location)
    title = _text_from_html(event.get("title"))
    path = event.get("fullUrl")
    start_ms = event.get("startDate")
    if not all((title, path, start_ms, venue, city)):
        return None

    start = datetime.fromtimestamp(start_ms / 1000, tz=ZoneInfo("UTC")).astimezone(
        LOCAL_TIMEZONE
    )
    end_ms = event.get("endDate")
    end = (
        datetime.fromtimestamp(end_ms / 1000, tz=ZoneInfo("UTC")).astimezone(
            LOCAL_TIMEZONE
        )
        if end_ms
        else None
    )
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": urljoin(SOURCE_URL, path),
        "time_from": start.strftime("%H:%M"),
        "time_to": end.strftime("%H:%M") if end else None,
        "venue": venue,
        "city": city,
        "country_code": "US",
        "description": _text_from_html(event.get("body")),
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


class SaltBayChamberfestCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="saltbaychamberfest_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)"
        )
        records = []
        seen_event_ids = set()
        page_url = COLLECTION_URL
        seen_pages = set()

        while page_url and page_url not in seen_pages:
            seen_pages.add(page_url)
            log_message("Fetching event page", event="crawler_url_fetch", url=page_url)
            response = session.get(page_url, params={"format": "json"}, timeout=30)
            response.raise_for_status()
            payload = response.json()

            for event in [*(payload.get("upcoming") or []), *(payload.get("past") or [])]:
                event_id = event.get("id")
                if event_id and event_id in seen_event_ids:
                    continue
                if event_id:
                    seen_event_ids.add(event_id)
                record = _event_to_record(event)
                if record:
                    records.append(record)

            next_path = (payload.get("pagination") or {}).get("nextPageUrl")
            page_url = urljoin(SOURCE_URL, next_path) if next_path else None

        log_message(
            "Scraped event collection",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    SaltBayChamberfestCrawler().run()


if __name__ == "__main__":
    main()
