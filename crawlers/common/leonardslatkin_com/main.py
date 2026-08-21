"""Crawler for Leonard Slatkin's international performance schedule."""

from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Leonard Slatkin"
SOURCE_URL = "https://www.leonardslatkin.com/"
EVENTS_API = f"{SOURCE_URL}wp-json/tribe/events/v1/events"

# Values currently emitted by the site's Events Calendar venue records.
COUNTRY_CODES = {
    "Canada": "CA",
    "China": "CN",
    "Czech Republic": "CZ",
    "Finland": "FI",
    "France": "FR",
    "Germany": "DE",
    "Hong Kong": "HK",
    "Hungary": "HU",
    "Ireland": "IE",
    "Israel": "IL",
    "Italy": "IT",
    "Japan": "JP",
    "Korea, Republic of": "KR",
    "Monaco": "MC",
    "Netherlands": "NL",
    "Norway": "NO",
    "Poland": "PL",
    "Romania": "RO",
    "Russian Federation": "RU",
    "Singapore": "SG",
    "Spain": "ES",
    "Switzerland": "CH",
    "Taiwan": "TW",
    "Turkey": "TR",
    "United Kingdom": "GB",
    "United States": "US",
}


def clean_text(value: str | None) -> str | None:
    """Turn an HTML fragment into readable multiline text."""
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    for unwanted in soup(["script", "style"]):
        unwanted.decompose()
    lines = [" ".join(line.split()) for line in soup.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line)
    return unescape(text) or None


class LeonardSlatkinCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="leonardslatkin_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "ClassicalBot/1.0 (+https://classical.bot/)"}
        )

    def _get_page(self, page: int) -> dict:
        params = {
            "start_date": "2000-01-01 00:00:00",
            "end_date": "2100-12-31 23:59:59",
            "per_page": 50,
            "page": page,
            "status": "publish",
        }
        log_message(
            "Fetching schedule API page",
            event="crawler_url_fetch",
            url=EVENTS_API,
            page=page,
        )
        response = self.session.get(EVENTS_API, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse_event(event: dict) -> dict | None:
        venue = event.get("venue") or {}
        venue_name = clean_text(venue.get("venue"))
        city = clean_text(venue.get("city"))
        country = (venue.get("country") or "").strip()
        country_code = country.upper() if len(country) == 2 else COUNTRY_CODES.get(country)

        start = event.get("start_date") or ""
        title = clean_text(event.get("title"))
        url = event.get("url")
        if not (title and url and len(start) >= 16 and venue_name and city and country_code):
            log_message(
                "Skipping event with incomplete required fields",
                event="crawler_record_skipped",
                url=url,
                event_id=event.get("id"),
            )
            return None

        date = start[:10]
        time_from = None if event.get("all_day") else start[11:16]
        end = event.get("end_date") or ""
        time_to = None
        if not event.get("all_day") and len(end) >= 16 and end[11:16] != time_from:
            time_to = end[11:16]

        return {
            "title": title,
            "date": date,
            "url": url,
            "time_from": time_from,
            "time_to": time_to,
            "venue": venue_name,
            "city": city,
            "country_code": country_code,
            "description": clean_text(event.get("description")),
        }

    def scrape(self) -> list[dict]:
        first_page = self._get_page(1)
        total_pages = int(first_page.get("total_pages") or 1)
        events = list(first_page.get("events") or [])

        for page in range(2, total_pages + 1):
            events.extend(self._get_page(page).get("events") or [])

        records = []
        for event in events:
            record = self._parse_event(event)
            if record is not None:
                records.append(record)

        log_message(
            "Schedule API scrape completed",
            event="crawler_api_scrape_completed",
            record_count=len(records),
            source_record_count=len(events),
            page_count=total_pages,
        )
        return records


def main():
    LeonardSlatkinCrawler().run()


if __name__ == "__main__":
    main()
