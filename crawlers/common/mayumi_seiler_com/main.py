import html
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Mayumi Seiler"
SOURCE_URL = "https://www.mayumi-seiler.com/"
API_URL = "https://www.mayumi-seiler.com/wp-json/tribe/events/v1/events"
COUNTRY_CODES = {
    "Canada": "CA",
    "France": "FR",
    "Germany": "DE",
    "Japan": "JP",
    "Switzerland": "CH",
    "United Arab Emirates": "AE",
    "United Kingdom": "GB",
    "United States": "US",
}


def clean_text(value):
    if not value:
        return None
    text = BeautifulSoup(html.unescape(value), "html.parser").get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line) or None


def get_events():
    events = []
    page = 1

    while True:
        log_message(
            "Fetching events API page",
            event="crawler_url_fetch",
            url=API_URL,
            page=page,
        )
        response = requests.get(
            API_URL,
            params={
                "start_date": "1900-01-01 00:00:00",
                "per_page": 50,
                "page": page,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get("events", []))

        if page >= payload.get("total_pages", 1):
            break
        page += 1

    return events


def event_to_record(event):
    start = datetime.strptime(event["start_date"], "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(event["end_date"], "%Y-%m-%d %H:%M:%S")
    # Multi-day, all-day entries on this artist calendar are seminar/overview
    # listings rather than concrete performance occurrences.
    if event.get("all_day") and end.date() > start.date():
        return None

    venue = event.get("venue")
    if not isinstance(venue, dict):
        return None

    venue_name = clean_text(venue.get("venue"))
    city = clean_text(venue.get("city"))
    country_code = COUNTRY_CODES.get(clean_text(venue.get("country")))
    if not venue_name or not city or not country_code:
        return None

    return {
        "title": clean_text(event.get("title")),
        "date": start.date().isoformat(),
        "url": event.get("url"),
        "time_from": None if event.get("all_day") else start.time().isoformat(timespec="minutes"),
        "venue": venue_name,
        "city": city,
        "country_code": country_code,
        "description": clean_text(event.get("description")),
    }


class MayumiSeilerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="mayumi_seiler_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date"],
    )

    def scrape(self):
        records = []
        for event in get_events():
            try:
                record = event_to_record(event)
            except (KeyError, TypeError, ValueError) as error:
                log_message(
                    "Skipping malformed event",
                    event="crawler_event_skipped",
                    url=event.get("url"),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record and all(record.get(field) for field in ("title", "date", "url")):
                records.append(record)

        log_message(
            "Parsed events",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    MayumiSeilerCrawler().run()


if __name__ == "__main__":
    main()
