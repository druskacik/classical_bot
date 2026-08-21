import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Jonathan Aner"
SOURCE_URL = "https://www.jonathananer.com/"
CONCERTS_URL = urljoin(SOURCE_URL, "#concerts")

COUNTRY_CODES = {
    "D": "DE",
    "DE": "DE",
    "AT": "AT",
}

# Most entries name only a town and cannot be emitted because the site supplies
# no venue. These exceptions are places explicitly named in the event text.
KNOWN_VENUES = {
    "schloss engers": ("Schloss Engers", "Neuwied"),
}


def _text(element) -> str:
    return element.get_text(" ", strip=True) if element else ""


def _parse_date(item) -> str | None:
    date_element = item.select_one(
        ".miga_simple_events_date:not(.miga_simple_events_dateTo)"
    )
    if not date_element:
        return None

    day = _text(date_element.select_one(".miga_simple_events_day"))
    month = _text(date_element.select_one(".miga_simple_events_month"))
    year = _text(date_element.select_one(".miga_simple_events_year"))
    try:
        return datetime.strptime(f"{day} {month} {year}", "%d %b %Y").date().isoformat()
    except ValueError:
        return None


def _parse_location(event_text: str) -> tuple[str, str, str] | None:
    """Return (venue, city, country_code) only when the venue is explicit."""
    country_match = re.search(r"\(\s*(D|DE|AT)\s*\)", event_text, flags=re.IGNORECASE)
    if not country_match:
        return None
    country_code = COUNTRY_CODES[country_match.group(1).upper()]

    location_text = event_text[: country_match.start()].strip(" –-")
    venue_match = re.search(r"\bin\s+(.+)$", event_text[country_match.end() :], re.IGNORECASE)
    if venue_match and location_text:
        venue = venue_match.group(1).strip(" .–-")
        if venue:
            return venue, location_text, country_code

    known = KNOWN_VENUES.get(location_text.casefold())
    if known:
        venue, city = known
        return venue, city, country_code
    return None


class JonathanAnerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jonathananer_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="DE",
        upload_target="potential",
        front_fields=[
            ("source_url", SOURCE_URL),
            ("source", SOURCE),
        ],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=SOURCE_URL)
        try:
            response = requests.get(
                SOURCE_URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                "Concert calendar fetch failed",
                event="crawler_url_fetch_failed",
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, "html.parser")
        records = []
        for item in soup.select(".miga_simple_events_ul > li"):
            title = _text(item.select_one(".miga_simple_events_text_content"))
            event_date = _parse_date(item)
            location = _parse_location(title)
            if not title or not event_date or not location:
                continue

            venue, city, country_code = location
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": CONCERTS_URL,
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": None,
                }
            )

        log_message(
            "Concert calendar parsed",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    JonathanAnerCrawler().run()


if __name__ == "__main__":
    main()
