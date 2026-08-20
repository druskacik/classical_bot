import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Christopher Tin"
SOURCE_URL = "https://christophertin.com/"
EVENTS_URL = urljoin(SOURCE_URL, "pages/events")

COUNTRY_BY_REGION = {
    "AB": "CA",
    "BC": "CA",
    "MB": "CA",
    "NB": "CA",
    "NL": "CA",
    "NS": "CA",
    "NT": "CA",
    "NU": "CA",
    "ON": "CA",
    "PE": "CA",
    "QC": "CA",
    "SK": "CA",
    "YT": "CA",
    "UK": "GB",
}

US_REGIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

VENUE_OVERRIDES = {
    ("Montreal", "Opéra de Montreal"): "Salle Wilfrid-Pelletier, Place des Arts",
    ("Vilnius", "BEL CANTO CHORAS VILNIUS, LITHUANIAN STATE SYMPHONY ORCHESTRA AND FRIENDS"): "LSSO Concert Hall",
    ("Dallas", "The Dallas Opera"): "Winspear Opera House",
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _event_date(month_day: str, previous: date | None) -> date:
    """Resolve the site's yearless, chronologically ordered event dates."""
    parsed = datetime.strptime(month_day, "%b %d")
    today = date.today()
    candidate = date(today.year, parsed.month, parsed.day)

    if previous is None:
        # The page is an upcoming-events feed. Allow a short grace period for an
        # event that has just happened, but otherwise roll into the next year.
        if candidate < today and (today - candidate).days > 14:
            candidate = candidate.replace(year=candidate.year + 1)
    else:
        candidate = candidate.replace(year=previous.year)
        if candidate < previous:
            candidate = candidate.replace(year=previous.year + 1)

    return candidate


def _location(value: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in value.rsplit(",", 1)]
    if len(parts) != 2 or not all(parts):
        return None
    region = parts[1].upper()
    country_code = COUNTRY_BY_REGION.get(region)
    if country_code is None:
        country_code = "US" if region in US_REGIONS else region
    if not re.fullmatch(r"[A-Z]{2}", country_code):
        return None
    return parts[0].title(), country_code


class ChristopherTinCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="christophertin_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "city", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching events page", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(EVENTS_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        previous_date = None
        for item in soup.select("ol.events li.event"):
            date_element = item.select_one(".event__date")
            location_element = item.select_one(".event__location")
            venue_element = item.select_one(".event__venue")
            if not date_element or not location_element or not venue_element:
                continue

            location = _location(_clean(location_element.get_text(" ", strip=True)))
            venue = _clean(venue_element.get_text(" ", strip=True))
            if location is None or not venue:
                continue

            try:
                event_date = _event_date(
                    _clean(date_element.get_text(" ", strip=True)).title(), previous_date
                )
            except ValueError:
                continue
            previous_date = event_date

            link = item.select_one(".event__link a[href]")
            url = urljoin(EVENTS_URL, link["href"]) if link else EVENTS_URL
            city, country_code = location
            listed_venue = venue.split(" / ", 1)[0].strip()
            physical_venue = VENUE_OVERRIDES.get((city, listed_venue), listed_venue)
            records.append(
                {
                    "title": venue,
                    "date": event_date.isoformat(),
                    "url": url,
                    "time_from": None,
                    "venue": physical_venue,
                    "city": city,
                    "country_code": country_code,
                    "description": venue,
                }
            )

        log_message(
            "Parsed events page",
            event="crawler_scrape_completed",
            url=EVENTS_URL,
            record_count=len(records),
        )
        return records


def main():
    ChristopherTinCrawler().run()


if __name__ == "__main__":
    main()
