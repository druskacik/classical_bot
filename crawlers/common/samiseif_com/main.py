from datetime import datetime
import html

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Sami Seif"
SOURCE_URL = "https://www.samiseif.com/"
API_URL = f"{SOURCE_URL}wp-json/tribe/events/v1/events"

COUNTRY_CODES = {
    "czech republic": "CZ",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "lebanon": "LB",
    "netherlands": "NL",
    "united kingdom": "GB",
    "united states": "US",
    "united states of america": "US",
}


def clean_text(value):
    if value is None:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text("\n")
    lines = [" ".join(html.unescape(line).split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text or None


def parse_event(event):
    venue_data = event.get("venue")
    if not isinstance(venue_data, dict):
        return None

    title = clean_text(event.get("title"))
    url = event.get("url")
    venue = clean_text(venue_data.get("venue"))
    city = clean_text(venue_data.get("city"))
    country = clean_text(venue_data.get("country"))
    country_code = COUNTRY_CODES.get(country.casefold()) if country else None

    start_value = event.get("start_date")
    try:
        start = datetime.strptime(start_value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None

    if not all((title, url, venue, city, country_code)):
        return None

    all_day = bool(event.get("all_day"))
    time_from = None if all_day else start.strftime("%H:%M:%S")
    time_to = None
    if not all_day:
        try:
            end = datetime.strptime(event.get("end_date"), "%Y-%m-%d %H:%M:%S")
            if end.date() == start.date():
                time_to = end.strftime("%H:%M:%S")
        except (TypeError, ValueError):
            pass

    return {
        "title": title,
        "date": start.strftime("%Y-%m-%d"),
        "url": url,
        "time_from": time_from,
        "time_to": time_to,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": clean_text(event.get("description")),
    }


class SamiSeifCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="samiseif_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        records = []
        page = 1

        while True:
            params = {
                "page": page,
                "per_page": 50,
                "start_date": "2000-01-01 00:00:00",
                "end_date": "2100-12-31 23:59:59",
                "status": "publish",
            }
            log_message(
                "Fetching events API page",
                event="crawler_url_fetch",
                url=API_URL,
                page=page,
            )
            response = requests.get(API_URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()

            events = payload.get("events", [])
            for event in events:
                record = parse_event(event)
                if record is not None:
                    records.append(record)

            total_pages = int(payload.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1

        log_message(
            "Events API scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    SamiSeifCrawler().run()


if __name__ == "__main__":
    main()
