from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Nicholas Daniel"
SOURCE_URL = "https://nicholasdaniel.co.uk/"
API_URL = f"{SOURCE_URL}wp-json/tribe/events/v1/events"

COUNTRY_CODES = {
    "Belgium": "BE",
    "China": "CN",
    "Czech Republic": "CZ",
    "Denmark": "DK",
    "Estonia": "EE",
    "Finland": "FI",
    "Germany": "DE",
    "Ireland": "IE",
    "Macau": "MO",
    "Netherlands": "NL",
    "Spain": "ES",
    "United Kingdom": "GB",
    "United States": "US",
}

# A few older venue records omit their country even though their city makes it
# unambiguous. Unknown locations are skipped rather than guessed.
CITY_COUNTRY_CODES = {
    "Coldwaltham, Pulborough": "GB",
    "Delft": "NL",
    "Espergærde": "DK",
    "Leicester": "GB",
    "Middlesbrough": "GB",
    "Vemb": "DK",
}


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    for element in soup.select("[class*='st_'][class*='_buttons']"):
        element.decompose()
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return unescape(text) or None


class NicholasDanielCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nicholasdaniel_co_uk",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GB",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        records = []
        page = 1
        total_pages = 1

        with requests.Session() as session:
            while page <= total_pages:
                params = {
                    "start_date": "2000-01-01",
                    "end_date": "2100-12-31",
                    "per_page": 50,
                    "page": page,
                    "status": "publish",
                }
                log_message("Fetching events page", event="crawler_url_fetch", url=API_URL, page=page)
                response = session.get(API_URL, params=params, timeout=30)
                response.raise_for_status()
                payload = response.json()
                total_pages = int(payload.get("total_pages", 1))

                for event in payload.get("events", []):
                    record = self._parse_event(event)
                    if record is not None:
                        records.append(record)
                page += 1

        log_message("Events parsed", event="crawler_records_parsed", record_count=len(records))
        return records

    @staticmethod
    def _parse_event(event: dict) -> dict | None:
        venue_data = event.get("venue") or {}
        venue = clean_text(venue_data.get("venue"))
        city = clean_text(venue_data.get("city"))
        country_code = COUNTRY_CODES.get(venue_data.get("country")) or CITY_COUNTRY_CODES.get(city)
        start = event.get("start_date", "")
        title = clean_text(event.get("title"))
        url = event.get("url")

        if not (title and url and venue and city and country_code and len(start) >= 10):
            log_message(
                "Skipping event with incomplete location or date",
                event="crawler_record_skipped",
                url=url,
                event_id=event.get("id"),
            )
            return None

        try:
            event_date = date.fromisoformat(start[:10]).isoformat()
        except ValueError:
            log_message(
                "Skipping event with invalid date",
                event="crawler_record_skipped",
                url=url,
                event_id=event.get("id"),
            )
            return None

        time_from = None if event.get("all_day") else start[11:16] or None
        end = event.get("end_date", "")
        time_to = None
        if not event.get("all_day") and len(end) >= 16 and end[:10] == event_date:
            candidate = end[11:16]
            if candidate and candidate != time_from:
                time_to = candidate

        return {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "time_to": time_to,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": clean_text(event.get("description")),
        }


def main():
    NicholasDanielCrawler().run()


if __name__ == "__main__":
    main()
