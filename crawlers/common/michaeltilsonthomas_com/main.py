"""Crawler for performances of Michael Tilson Thomas's compositions."""

import calendar
import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Michael Tilson Thomas"
SOURCE_URL = "https://michaeltilsonthomas.com/"
PERFORMANCES_URL = urljoin(SOURCE_URL, "performances/")

MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
COUNTRY_CODES = {
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "canada": "CA",
    "china": "CN",
    "czech republic": "CZ",
    "denmark": "DK",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "hong kong": "HK",
    "ireland": "IE",
    "israel": "IL",
    "italy": "IT",
    "japan": "JP",
    "mexico": "MX",
    "netherlands": "NL",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "singapore": "SG",
    "south korea": "KR",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "taiwan": "TW",
    "united kingdom": "GB",
    "uk": "GB",
    "united states": "US",
    "usa": "US",
}


def clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value.get_text(" ", strip=True)).strip()


def parse_location(value: str) -> tuple[str, str] | None:
    """Return city and ISO country code from the site's compact location text."""
    text = clean_text(BeautifulSoup(value, "html.parser")).lstrip("• ").strip()
    parts = [part.strip() for part in text.rsplit(",", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None

    city, region = parts
    normalized = region.casefold().rstrip(".")
    if region.upper() in US_STATES:
        return city, "US"
    country_code = COUNTRY_CODES.get(normalized)
    if not country_code and len(region) == 2 and region.isalpha():
        country_code = region.upper()
    return (city, country_code) if country_code else None


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_dates(value: str) -> list[str]:
    """Expand the single dates and inclusive ranges used by the calendar."""
    text = re.sub(r"\s+", " ", value.replace("–", "-").replace("—", "-")).strip()
    match = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})(?:\s*(?:&|-)\s*(?:(?:([A-Za-z]+)\s+)?(\d{1,2})))?[,.]?\s+(\d{4})",
        text,
    )
    if not match:
        return []

    first_month_name, first_day, second_month_name, second_day, year = match.groups()
    first_month = MONTHS.get(first_month_name.casefold())
    second_month = MONTHS.get((second_month_name or first_month_name).casefold())
    if not first_month or not second_month:
        return []

    start = _make_date(int(year), first_month, int(first_day))
    if not start:
        return []
    if not second_day:
        return [start.isoformat()]

    end = _make_date(int(year), second_month, int(second_day))
    if not end or end < start or (end - start).days > 31:
        return []
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]


def parse_event(item) -> list[dict]:
    title = clean_text(item.select_one(".event-title")).rstrip(":").strip()
    venue = clean_text(item.select_one(".upcoming-venue:not(.location)"))
    location_node = item.select_one(".upcoming-venue.location")
    date_node = item.select_one(".upcoming-dates")
    link = item.select_one("a.ticket-link[href]")
    dates = parse_dates(clean_text(date_node))
    location = parse_location(str(location_node)) if location_node else None
    url = urljoin(PERFORMANCES_URL, link.get("href", "")) if link else ""

    if not (title and venue and location and dates and url):
        log_message(
            "Skipping performance with incomplete required fields",
            event="crawler_record_skipped",
            level="warning",
            url=url or PERFORMANCES_URL,
            event_id=item.get("id"),
        )
        return []

    city, country_code = location
    notes = item.select_one(".upcoming-notes")
    description = clean_text(notes) or None
    return [
        {
            "title": title,
            "date": performance_date,
            "url": url,
            "time_from": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
            "source_url": SOURCE_URL,
            "source": SOURCE,
        }
        for performance_date in dates
    ]


class MichaelTilsonThomasComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="michaeltilsonthomas_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        columns=[
            "title", "date", "url", "time_from", "venue", "city",
            "country_code", "description", "source_url", "source",
        ],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "ClassicalBot/1.0 (+https://classical.bot/)"}
        )

    def scrape(self) -> list[dict]:
        response = self.session.get(PERFORMANCES_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        # The source currently emits two ``class`` attributes on each card.
        # HTML parsers may retain only the second one, so use its stable post id
        # and data-category attributes instead of the visual .events-item class.
        items = soup.select(".eventHolder > li[id^='post-'][data-category]")
        records = [record for item in items for record in parse_event(item)]
        records.sort(key=lambda record: (record["date"], record["title"], record["venue"]))
        log_message(
            "Performance calendar scrape completed",
            event="crawler_scrape_completed",
            url=PERFORMANCES_URL,
            source_record_count=len(items),
            record_count=len(records),
        )
        return records


def main():
    MichaelTilsonThomasComCrawler().run()


if __name__ == "__main__":
    main()
