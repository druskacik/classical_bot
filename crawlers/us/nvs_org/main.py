import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.nvs.org/"
EVENTS_URL = urljoin(SOURCE_URL, "upcoming-events")
EVENTS_API_URL = f"{EVENTS_URL}?format=json"
SOURCE = "Nittany Valley Symphony"
COUNTRY_CODE = "US"
LOCAL_TIMEZONE = ZoneInfo("America/New_York")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# These entries omit Squarespace's structured location even though their body
# names the venue. The mapping is deliberately limited to an exact venue name.
VENUE_CITIES = {
    "Schlow Library": "State College",
}


def clean_text(value):
    if not value:
        return ""
    text = BeautifulSoup(html.unescape(str(value)), "html.parser").get_text("\n", strip=True)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_start_date(value):
    try:
        timestamp = int(value) / 1000
        local_datetime = datetime.fromtimestamp(timestamp, tz=LOCAL_TIMEZONE)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return local_datetime.date().isoformat(), local_datetime.strftime("%H:%M")


def city_from_location(location):
    address_line = clean_text(location.get("addressLine2"))
    if not address_line:
        return None
    # Squarespace stores values such as "Bellefonte, PA, 16823" and
    # "University Park PA 16802" in the same field.
    match = re.match(r"^(.+?)(?:,\s*|\s+)[A-Z]{2}(?:,?\s+\d{5})?\s*$", address_line)
    return clean_text(match.group(1)) if match else None


def location_from_item(item, description):
    location = item.get("location") or {}
    venue = clean_text(location.get("addressTitle"))
    city = city_from_location(location)
    country = clean_text(location.get("addressCountry"))
    if venue and city and country.casefold() in {"united states", "us", "usa"}:
        return venue, city

    for known_venue, known_city in VENUE_CITIES.items():
        if re.search(rf"\b{re.escape(known_venue)}\b", description or "", re.IGNORECASE):
            return known_venue, known_city
    return None


def description_from_item(item):
    parts = []
    for field in ("body", "excerpt"):
        value = clean_text(item.get(field))
        if value and value not in parts:
            parts.append(value)
    return "\n\n".join(parts) or None


def parse_item(item):
    title = clean_text(item.get("title"))
    parsed_start = parse_start_date(item.get("startDate"))
    path = clean_text(item.get("fullUrl"))
    description = description_from_item(item)
    location = location_from_item(item, description)
    if not title or not parsed_start or not path or not location:
        return None

    event_date, time_from = parsed_start
    venue, city = location
    return {
        "title": title,
        "date": event_date,
        "url": urljoin(SOURCE_URL, path),
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": COUNTRY_CODE,
        "description": description,
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


class NvsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nvs_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target="potential",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "venue",
            "city",
            "country_code",
            "description",
            "source_url",
            "source",
        ],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        try:
            response = requests.get(EVENTS_API_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                "Failed to fetch Nittany Valley Symphony event feed",
                event="crawler_fetch_failed",
                level="error",
                url=EVENTS_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        items = [*(payload.get("upcoming") or []), *(payload.get("past") or [])]
        records = []
        for item in items:
            record = parse_item(item)
            if record:
                records.append(record)
            else:
                log_message(
                    "Skipping event without a valid date, venue, or city",
                    event="crawler_record_skipped",
                    level="warning",
                    url=urljoin(SOURCE_URL, clean_text(item.get("fullUrl"))),
                )

        log_message(
            "Nittany Valley Symphony event feed parsed",
            event="crawler_parse_completed",
            url=EVENTS_API_URL,
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda record: (
                record["date"], record["time_from"] or "", record["title"], record["url"]
            ),
        )


def main():
    NvsOrgCrawler().run()


if __name__ == "__main__":
    main()
