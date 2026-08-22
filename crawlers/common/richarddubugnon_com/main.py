import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.richarddubugnon.com/"
SOURCE = "Richard Dubugnon"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar-1")
SITE_TIMEZONE = ZoneInfo("Europe/Paris")

COUNTRY_CODES = {
    "Austria": "AT",
    "Belgium": "BE",
    "China": "CN",
    "Finlande": "FI",
    "France": "FR",
    "Germany": "DE",
    "Japan": "JP",
    "Liechtenstein": "LI",
    "Luxembourg": "LU",
    "Netherlands": "NL",
    "Spain": "ES",
    "Switzerland": "CH",
    "Taiwan": "TW",
    "United Arab Emirates": "AE",
    "United Kingdom": "GB",
}

# A handful of older entries did not use Squarespace's structured location
# fields. These values are stated explicitly in the event body.
LOCATION_FALLBACKS = {
    "world-premiere-of-passacaille-concertante-5a5f9": ("Kirche", "Windisch", "CH"),
    "belgian-premiere-of-arcanes-symphoniques": ("Bozar", "Brussels", "BE"),
    "mikroncerto-i-for-double-bass-and-orchestra": ("Muziekgebouw het IJ", "Amsterdam", "NL"),
    "event-five-m8rng": ("Kunsthalle", "Appenzell", "CH"),
    "event-two-fesah": ("Kuhmo Arts Centre", "Kuhmo", "FI"),
}


def _description(body: str) -> str | None:
    soup = BeautifulSoup(body or "", "html.parser")
    content = soup.select_one(".sqs-html-content") or soup
    for element in content.select("style, script"):
        element.decompose()
    text = re.sub(r"\s+", " ", content.get_text(" ", strip=True)).strip()
    return text or None


def _city(address_line: str) -> str | None:
    address_line = html.unescape(address_line or "").strip()
    if not address_line:
        return None

    parts = [part.strip() for part in address_line.split(",") if part.strip()]
    first = parts[0]
    # Most Squarespace addresses put the city first. Some omit commas and put
    # a postal code before it; one archived German address puts the city in the
    # second comma-delimited component.
    if len(parts) > 1 and re.match(r"^\d{4,6}\s+\D", parts[1]):
        first = parts[1]
    first = re.sub(r"^\d{4,6}(?:\s+[A-Z]{1,2})?\s+", "", first).strip()
    first = re.sub(r"^\d{4,6}\s*", "", first).strip()
    return first or None


def _location(item: dict) -> tuple[str, str, str] | None:
    location = item.get("location") or {}
    venue = html.unescape((location.get("addressTitle") or "").strip())
    city = _city(location.get("addressLine2") or "")
    country_code = COUNTRY_CODES.get((location.get("addressCountry") or "").strip())

    fallback = LOCATION_FALLBACKS.get(item.get("urlId"))
    if fallback:
        venue = venue or fallback[0]
        city = city or fallback[1]
        country_code = country_code or fallback[2]

    if not venue or not city or not country_code:
        return None
    return venue, city, country_code


def _time(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=SITE_TIMEZONE)


class RichardDubugnonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="richarddubugnon_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        seen_ids = set()
        offset = None

        while True:
            params = {"format": "json"}
            if offset is not None:
                params["offset"] = offset
            log_message(
                "Fetching calendar page",
                event="crawler_url_fetch",
                url=CALENDAR_URL,
                offset=offset,
            )
            response = requests.get(CALENDAR_URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()

            for item in payload.get("upcoming", []) + payload.get("past", []):
                if item.get("id") in seen_ids:
                    continue
                seen_ids.add(item.get("id"))

                location = _location(item)
                if location is None:
                    log_message(
                        "Skipping event without a complete location",
                        event="crawler_item_skipped",
                        url=urljoin(SOURCE_URL, item.get("fullUrl") or ""),
                    )
                    continue

                start_ms = item.get("startDate")
                if not isinstance(start_ms, (int, float)):
                    continue
                start = _time(start_ms)
                end_ms = item.get("endDate")
                end = _time(end_ms) if isinstance(end_ms, (int, float)) else None
                venue, city, country_code = location
                records.append(
                    {
                        "title": (item.get("title") or "").strip(),
                        "date": start.date().isoformat(),
                        "url": urljoin(SOURCE_URL, item.get("fullUrl") or ""),
                        "time_from": start.strftime("%H:%M"),
                        "time_to": end.strftime("%H:%M") if end and end.date() == start.date() else None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": _description(item.get("body") or ""),
                    }
                )

            pagination = payload.get("pagination") or {}
            next_offset = pagination.get("nextPageOffset") if pagination.get("nextPage") else None
            if next_offset is None or next_offset == offset:
                break
            offset = next_offset

        log_message(
            "Calendar events parsed",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    RichardDubugnonCrawler().run()


if __name__ == "__main__":
    main()
