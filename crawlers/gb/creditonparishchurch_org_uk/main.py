from datetime import datetime
from html import unescape
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Crediton Parish Church"
SOURCE_URL = "https://www.creditonparishchurch.org.uk/about-2/music/choir/"
API_URL = "https://www.creditonparishchurch.org.uk/wp-json/my-calendar/v1/events"
DETAIL_URL = "https://www.creditonparishchurch.org.uk/"


def clean_text(value):
    if not value:
        return None
    text = BeautifulSoup(
        unescape(str(value)).replace("\\'", "'"), "html.parser"
    ).get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned or None


def event_url(event):
    query = urlencode({"p": event["event_post"], "mc_id": event["occur_id"]})
    return f"{DETAIL_URL}?{query}"


class CreditonParishChurchCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="creditonparishchurch_org_uk",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GB",
        upload_target="potential",
        dedupe_subset=["date", "time_from", "url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        params = {"from": "1900-01-01", "to": "2100-12-31"}
        log_message(
            "Fetching calendar events",
            event="crawler_url_fetch",
            url=API_URL,
        )
        response = requests.get(API_URL, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Calendar API returned an unexpected response")

        records = []
        skipped_missing_venue = 0
        for events in payload.values():
            if not isinstance(events, list):
                continue
            for event in events:
                location = event.get("location") or {}
                venue = clean_text(location.get("location_label"))
                if not venue:
                    skipped_missing_venue += 1
                    continue

                starts_at = datetime.strptime(
                    event["occur_begin"], "%Y-%m-%d %H:%M:%S"
                )
                ends_at = datetime.strptime(
                    event["occur_end"], "%Y-%m-%d %H:%M:%S"
                )
                records.append(
                    {
                        "title": clean_text(event.get("event_title")),
                        "date": starts_at.date().isoformat(),
                        "url": event_url(event),
                        "time_from": starts_at.time().isoformat(),
                        "time_to": (
                            None
                            if event.get("event_hide_end") == "1"
                            else ends_at.time().isoformat()
                        ),
                        "venue": venue,
                        "city": "Crediton",
                        "description": clean_text(event.get("event_desc")),
                    }
                )

        if skipped_missing_venue:
            log_message(
                "Skipped events without a defensible venue",
                event="crawler_records_skipped",
                level="warning",
                record_count=skipped_missing_venue,
            )
        return records


def main():
    CreditonParishChurchCrawler().run()


if __name__ == "__main__":
    main()
