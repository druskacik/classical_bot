from datetime import datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Lavinia Meijer"
SOURCE_URL = "https://www.laviniameijer.com/"
API_URL = "https://rest.bandsintown.com/V3.1/artists/Lavinia%20Meijer/events"
REQUEST_PARAMS = {
    "app_id": "js_www.laviniameijer.com",
    "date": "upcoming",
}
HEADERS = {
    "Accept": "application/json",
    "Origin": SOURCE_URL.rstrip("/"),
    "Referer": SOURCE_URL,
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)",
}
COUNTRY_CODES = {
    "Austria": "AT",
    "Belgium": "BE",
    "Canada": "CA",
    "Denmark": "DK",
    "Finland": "FI",
    "France": "FR",
    "Germany": "DE",
    "Ireland": "IE",
    "Israel": "IL",
    "Italy": "IT",
    "Japan": "JP",
    "Luxembourg": "LU",
    "Netherlands": "NL",
    "Norway": "NO",
    "Portugal": "PT",
    "South Korea": "KR",
    "Spain": "ES",
    "Sweden": "SE",
    "Switzerland": "CH",
    "United Kingdom": "GB",
    "United States": "US",
}


def _clean(value):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _parse_datetime(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class LaviniaMeijerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="laviniameijer_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NL",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[
            ("source_url", SOURCE_URL),
            ("source", SOURCE),
        ],
    )

    def scrape(self):
        log_message("Fetching concert feed", event="crawler_url_fetch", url=API_URL)
        response = requests.get(
            API_URL,
            params=REQUEST_PARAMS,
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        events = response.json()
        if not isinstance(events, list):
            raise ValueError("Bandsintown response is not an event list")

        records = []
        for event in events:
            record = self._parse_event(event)
            if record is None:
                log_message(
                    "Skipping incomplete concert",
                    event="crawler_record_skipped",
                    url=event.get("url") if isinstance(event, dict) else None,
                )
                continue
            records.append(record)

        log_message(
            "Concert feed parsed",
            event="crawler_feed_parsed",
            url=API_URL,
            record_count=len(records),
        )
        return records

    @staticmethod
    def _parse_event(event):
        if not isinstance(event, dict):
            return None

        start = _parse_datetime(event.get("datetime") or event.get("starts_at"))
        end = _parse_datetime(event.get("ends_at"))
        venue_data = event.get("venue")
        if not start or not isinstance(venue_data, dict):
            return None

        title = _clean(event.get("title"))
        url = _clean(event.get("url"))
        venue = _clean(venue_data.get("name"))
        city = _clean(venue_data.get("city"))
        country = _clean(venue_data.get("country"))
        country_code = (
            country.upper()
            if country and len(country) == 2
            else COUNTRY_CODES.get(country)
        )
        if not all((title, url, venue, city, country_code)):
            return None

        return {
            "title": title,
            "date": start.date().isoformat(),
            "url": url,
            "time_from": start.time().replace(tzinfo=None).isoformat(timespec="minutes"),
            "time_to": (
                end.time().replace(tzinfo=None).isoformat(timespec="minutes")
                if end
                else None
            ),
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": _clean(event.get("description")),
        }


def main():
    LaviniaMeijerCrawler().run()


if __name__ == "__main__":
    main()
