from datetime import datetime, timedelta, timezone
import html
import re

from bs4 import BeautifulSoup
import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Lara St. John"
SOURCE_URL = "https://www.larastjohn.com/"
EVENTS_API_URL = "https://www.larastjohn.com/events"
SITE_TIMEZONE = timezone(timedelta(hours=8))

COUNTRY_CODES = {
    "argentina": "AR",
    "buenos aires": "AR",
    "canada": "CA",
    "colombia": "CO",
    "czech republic": "CZ",
    "estonia": "EE",
    "finland": "FI",
    "ireland": "IE",
    "israel": "IL",
    "italy": "IT",
    "kazakhstan": "KZ",
    "mexico": "MX",
    "peru": "PE",
    "poland": "PL",
    "spain": "ES",
    "spain 30006": "ES",
    "sweden": "SE",
    "united kingdon": "GB",
    "united kingdom": "GB",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
}


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text or None


def country_code(value: str | None) -> str | None:
    normalized = clean_text(value).casefold()
    return COUNTRY_CODES.get(normalized)


def parse_city(location: dict, code: str) -> str | None:
    line = clean_text(location.get("addressLine2"))
    parts = [part.strip() for part in line.split(",") if part.strip()]
    if not parts:
        return None

    city = parts[0]
    if city[:1].isdigit() and len(parts) > 1:
        city = parts[1]

    if code == "AR" and re.match(r"^[A-Z]?\d", city, re.I):
        city = "Buenos Aires"
    elif code == "US":
        city = re.sub(r"\s+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$", "", city, flags=re.I)
    elif code == "CA":
        city = re.sub(
            r"\s+[A-Z]{2}(?:\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d)?$",
            "",
            city,
            flags=re.I,
        )
    elif code == "GB":
        city = re.sub(r"\s+[A-Z]{1,2}\d[A-Z\d]?\s*\d?[A-Z]{0,2}$", "", city, flags=re.I)
    else:
        city = re.sub(r"^\d{4,6}\s+", "", city)
        city = re.sub(r"\s+\d[\d ]*$", "", city)

    # One malformed UK record puts only the second half of a postcode in line 2.
    if not city or re.fullmatch(r"[A-Z0-9]{2,4}(?:\s+UK)?", city, re.I):
        line1_parts = [
            part.strip()
            for part in clean_text(location.get("addressLine1")).split(",")
            if part.strip()
        ]
        if line1_parts:
            city = re.sub(
                r"\s+[A-Z]{1,2}\d[A-Z\d]?$", "", line1_parts[-1], flags=re.I
            )

    city = city.strip(" ,")
    return city or None


def parse_event(item: dict) -> dict | None:
    location = item.get("location") or {}
    venue = clean_text(location.get("addressTitle"))
    code = country_code(location.get("addressCountry"))
    city = parse_city(location, code) if code else None
    title = clean_text(item.get("title"))
    path = item.get("fullUrl")
    start_ms = item.get("startDate")

    # Free-form excerpts frequently contain an address, but guessing its fields
    # would create invalid geography. Keep only Squarespace's structured places.
    if not all((title, path, start_ms, venue, city, code)):
        return None

    start = datetime.fromtimestamp(start_ms / 1000, tz=SITE_TIMEZONE)
    end_ms = item.get("endDate")
    end = (
        datetime.fromtimestamp(end_ms / 1000, tz=SITE_TIMEZONE)
        if end_ms is not None
        else None
    )

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": requests.compat.urljoin(SOURCE_URL, path),
        "time_from": start.strftime("%H:%M:%S"),
        "time_to": end.strftime("%H:%M:%S") if end else None,
        "venue": venue,
        "city": city,
        "country_code": code,
        "description": html_to_text(item.get("body") or item.get("excerpt")),
    }


class LaraStJohnCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="larastjohn_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        records = []
        offset = None
        seen_offsets = set()

        while True:
            params = {"format": "json"}
            if offset is not None:
                params["offset"] = offset

            log_message(
                "Fetching event page",
                event="crawler_url_fetch",
                url=EVENTS_API_URL,
                offset=offset,
            )
            response = session.get(EVENTS_API_URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()

            items = (payload.get("upcoming") or []) + (payload.get("past") or [])
            records.extend(record for item in items if (record := parse_event(item)))

            pagination = payload.get("pagination") or {}
            if not pagination.get("nextPage"):
                break
            offset = pagination.get("nextPageOffset")
            if offset is None or offset in seen_offsets:
                break
            seen_offsets.add(offset)

        log_message(
            "Parsed event catalogue",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    LaraStJohnCrawler().run()


if __name__ == "__main__":
    main()
