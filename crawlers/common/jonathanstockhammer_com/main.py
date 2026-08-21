import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Jonathan Stockhammer"
SOURCE_URL = "https://jonathanstockhammer.com/"
CALENDAR_URLS = (
    urljoin(SOURCE_URL, "calendar/"),
    urljoin(SOURCE_URL, "calendar/archive/"),
)

# Older entries occasionally omit the city because it is part of a well-known
# venue name. These are all venue strings used by the site's current archive.
VENUE_CITIES = {
    "Alte Oper Frankfurt": "Frankfurt",
    "Barbican Hall": "London",
    "Deutsche Oper Berlin": "Berlin",
    "Die Bayreuther Festspiele": "Bayreuth",
    "Die Glocke": "Bremen",
    "Elbphilharmonie": "Hamburg",
    "Elisabethkirche Berlin": "Berlin",
    "E-Werk": "Freiburg",
    "Hellerau": "Dresden",
    "HR-Sendesaal Frankfurt": "Frankfurt",
    "Kölner Philharmonie": "Köln",
    "Konzerthaus Berlin": "Berlin",
    "Križevniška Church": "Ljubljana",
    "Musikverein Wien": "Wien",
    "New National Theatre Tokyo Opera Studio": "Tokyo",
    "Opera Antwerp": "Antwerp",
    "Opernhaus Frankfurt": "Frankfurt",
    "Opernhaus Zürich": "Zürich",
    "Philharmonie Essen": "Essen",
    "Stadthalle Reutlingen": "Reutlingen",
    "Studio der WPR Reutlingen": "Reutlingen",
    "Theater Dortmund": "Dortmund",
    "Turku Concert hall": "Turku",
}
COUNTRY_CODES = {"UK": "GB", "SL": "SI"}


def clean_text(element) -> str:
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def parse_date(value: str) -> str:
    normalized = value.replace("’", "'").replace("‘", "'")
    return datetime.strptime(normalized, "%d %b '%y").date().isoformat()


def parse_location(value: str) -> tuple[str, str, str] | None:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) >= 3:
        venue = ", ".join(parts[:-2])
        city = parts[-2]
        country_code = parts[-1].upper()
    elif len(parts) == 2 and parts[-1].upper() in {"AT", "BE", "CH", "DE", "FI", "JP", "SL", "UK"}:
        venue = parts[0]
        city = VENUE_CITIES.get(venue)
        country_code = parts[1].upper()
        if city is None:
            return None
    elif len(parts) == 2 and parts[0] in VENUE_CITIES:
        venue = parts[0]
        city = parts[1]
        country_code = "DE"
    else:
        return None

    country_code = COUNTRY_CODES.get(country_code, country_code)
    if not re.fullmatch(r"[A-Z]{2}", country_code):
        return None
    return venue, city, country_code


def parse_calendar(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for item in soup.select(".calendar__item"):
        date_element = item.select_one(".date_large")
        location_element = item.select_one(".date_large + .date_small")
        performers_element = item.select_one(".black.is-4-tablet")
        programme_element = item.select_one(".is-one-fifth-desktop")
        if not all((date_element, location_element, performers_element)):
            log_message(
                "Skipping incomplete calendar entry",
                event="crawler_record_skipped",
                url=page_url,
                error_type="MissingRequiredElement",
            )
            continue

        location = parse_location(clean_text(location_element))
        if location is None:
            log_message(
                "Skipping calendar entry with unresolved location",
                event="crawler_record_skipped",
                url=page_url,
                error_type="UnresolvedLocation",
            )
            continue
        venue, city, country_code = location

        performers = clean_text(performers_element)
        date_text = clean_text(date_element)
        if re.search(r"\d\s*[-–]\s*\d", date_text) or performers.casefold() == "orchestre national de lyon recording":
            log_message(
                "Skipping non-performance calendar entry",
                event="crawler_record_skipped",
                url=page_url,
                error_type="NotConcretePerformance",
            )
            continue
        programme = clean_text(programme_element) if programme_element else ""
        description = "\n\n".join(part for part in (performers, programme) if part) or None
        link = item.select_one("a[href]")
        event_url = urljoin(page_url, link["href"]) if link else page_url

        records.append(
            {
                "title": performers,
                "date": parse_date(date_text),
                "url": event_url,
                "time_from": None,
                "time_to": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description,
            }
        )
    return records


class JonathanStockhammerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jonathanstockhammer_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "url", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)"
        for url in CALENDAR_URLS:
            log_message("Fetching calendar page", event="crawler_url_fetch", url=url)
            response = session.get(url, timeout=30)
            response.raise_for_status()
            page_records = parse_calendar(response.text, url)
            log_message(
                "Calendar page parsed",
                event="crawler_page_parsed",
                url=url,
                record_count=len(page_records),
            )
            records.extend(page_records)
        return records


def main():
    JonathanStockhammerCrawler().run()


if __name__ == "__main__":
    main()
