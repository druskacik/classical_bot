import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.moonarsenii.com/"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar")
SOURCE = "Arsenii Moon"

COUNTRY_CODES = {
    "austria": "AT",
    "france": "FR",
    "germany": "DE",
    "italy": "IT",
    "japan": "JP",
    "south korea": "KR",
    "switzerland": "CH",
    "united states": "US",
    "usa": "US",
}

# The calendar currently contains one incorrect country label (Graz, Italy).
# These unambiguous city corrections prevent that editorial typo from becoming
# bad geography while leaving all other records driven by their country label.
CITY_COUNTRIES = {
    "graz": "AT",
    "l'aquila": "IT",
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _calendar_year(soup: BeautifulSoup) -> int:
    """Infer the year shared by the site's month/day schedule."""
    year_counts: dict[int, int] = {}
    for link in soup.select(".user-items-list-item-container a[href]"):
        for raw_year in re.findall(r"(?<!\d)(20\d{2})(?!\d)", link.get("href", "")):
            year = int(raw_year)
            year_counts[year] = year_counts.get(year, 0) + 1

    if year_counts:
        return max(year_counts, key=lambda value: (year_counts[value], value))

    # The artist's home page describes seasons as e.g. 2025-26. For an
    # upcoming calendar, the latter year is the best interpretation.
    page_text = soup.get_text(" ", strip=True)
    season = re.search(r"(20\d{2})\s*[-–]\s*(\d{2,4})", page_text)
    if season:
        second = int(season.group(2))
        if second < 100:
            second += (int(season.group(1)) // 100) * 100
        return second

    return date.today().year


def _country_code(city: str, country: str | None) -> str | None:
    city_code = CITY_COUNTRIES.get(city.casefold())
    if city_code:
        return city_code
    if not country:
        return None
    return COUNTRY_CODES.get(country.casefold())


def _parse_card(card, year: int) -> dict | None:
    location_node = card.select_one(".list-item-content__title")
    description_node = card.select_one(".list-item-content__description")
    if not location_node or not description_node:
        return None

    location = _clean(location_node.get_text(" ", strip=True))
    location_parts = [_clean(part) for part in location.rsplit(",", 1)]
    city = location_parts[0]
    country = location_parts[1] if len(location_parts) == 2 else None
    country_code = _country_code(city, country)

    lines = [
        _clean(node.get_text(" ", strip=True))
        for node in description_node.select("p")
        if _clean(node.get_text(" ", strip=True))
    ]
    # Cards need a date, a named presenting venue/festival, and a separate
    # programme/performer line. With only two lines the second is ambiguous
    # (currently the L'Aquila card names an ensemble, not a venue).
    if len(lines) < 3 or not city or not country_code:
        return None

    try:
        event_date = datetime.strptime(f"{lines[0]} {year}", "%B %d %Y").date()
    except ValueError:
        return None

    venue = lines[1]
    if venue.casefold() == city.casefold():
        return None

    link = card.select_one("a[href]")
    href = link.get("href", "").strip() if link else ""
    event_url = urljoin(CALENDAR_URL, href) if href else CALENDAR_URL
    description = "\n".join(lines[1:]) or None

    return {
        "title": f"Arsenii Moon — {venue}",
        "date": event_date.isoformat(),
        "url": event_url,
        "time_from": None,
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class MoonArseniiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="moonarsenii_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "title", "city", "venue"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching performance calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.select(".user-items-list-item-container .list-item")
        year = _calendar_year(soup)

        records = []
        for card in cards:
            record = _parse_card(card, year)
            if record:
                records.append(record)
            else:
                log_message(
                    "Skipping calendar card with incomplete date or location",
                    event="crawler_record_skipped",
                    level="warning",
                )

        log_message(
            "Parsed performance calendar",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    MoonArseniiCrawler().run()


if __name__ == "__main__":
    main()
