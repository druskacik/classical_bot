import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://santafechambermusic.org/"
SOURCE = "Santa Fe Chamber Music Festival"
API_URL = f"{SOURCE_URL}wp-json/tribe/events/v1/events"
REQUEST_TIMEOUT = 45
PER_PAGE = 50


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(html.unescape(value))
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\[/?et_[^\]]*\]", "", text)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_event(event: dict) -> dict | None:
    title = clean_text(event.get("title"))
    url = event.get("url")
    start_value = event.get("start_date")
    venue_data = event.get("venue") or {}
    venue = clean_text(venue_data.get("venue"))
    city = clean_text(venue_data.get("city"))

    if not title or not url or not start_value or not venue or not city:
        return None

    try:
        start = datetime.strptime(start_value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None

    time_to = None
    end_value = event.get("end_date")
    if end_value and not event.get("all_day"):
        try:
            time_to = datetime.strptime(end_value, "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
        except (TypeError, ValueError):
            pass

    description = clean_text(event.get("description")) or clean_text(event.get("excerpt"))
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": None if event.get("all_day") else start.strftime("%H:%M"),
        "time_to": time_to,
        "venue": venue,
        "city": city,
        "country_code": "US",
        "description": description or None,
    }


class SantaFeChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="santafechambermusic_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/125.0 Safari/537.36"
                ),
            }
        )

        records = []
        page = 1
        while True:
            params = {
                "start_date": "1900-01-01",
                "end_date": "2100-12-31",
                "per_page": PER_PAGE,
                "page": page,
                "status": "publish",
            }
            try:
                response = session.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    "Failed to fetch Santa Fe Chamber Music Festival events",
                    event="crawler_fetch_failed",
                    level="error",
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get("events") or []
            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = payload.get("total_pages") or 0
            if page >= total_pages:
                break
            page += 1

        log_message(
            "Santa Fe Chamber Music Festival scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda record: (
                record["date"], record["time_from"] or "", record["title"], record["url"]
            ),
        )


def main():
    SantaFeChamberMusicOrgCrawler().run()


if __name__ == "__main__":
    main()
