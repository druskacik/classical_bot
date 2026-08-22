import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Reinhardt Buhr"
SOURCE_URL = "https://reinhardtbuhr.com/"
TOUR_URL = "https://reinhardtbuhr.com/en-eur/pages/tour-line-up"

COUNTRIES_BY_CITY = {
    "Berlin": "DE",
    "Bristol": "GB",
    "Cologne": "DE",
    "Frankfurt": "DE",
    "Hamburg": "DE",
    "London": "GB",
    "Munich": "DE",
    "Prague": "CZ",
    "Shrewsbury": "GB",
    "Vienna": "AT",
    "Warsaw": "PL",
}

# The source sometimes puts a country in the city position. These venue-specific
# corrections preserve the source's compact format without treating a country as
# a city.
LOCATION_CORRECTIONS = {
    ("Austria", "B72"): ("Vienna", "AT"),
    ("Czechia", "Rock Cafe"): ("Prague", "CZ"),
}


class ReinhardtBuhrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="reinhardtbuhr_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "url", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching tour page", event="crawler_url_fetch", url=TOUR_URL)
        response = requests.get(
            TOUR_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            },
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        current_year = None
        for section in soup.select('section[data-section-type="link-list"]'):
            heading = section.select_one(".link-list__heading")
            if heading:
                year_match = re.search(r"\b(20\d{2})\b", heading.get_text(" ", strip=True))
                if year_match:
                    current_year = int(year_match.group(1))
            if current_year is None:
                continue

            for link in section.select(".link-list__cta a[href]"):
                record = self._parse_link(
                    link.get_text(" ", strip=True), link["href"], current_year
                )
                if record:
                    records.append(record)

        log_message(
            "Tour page parsed",
            event="crawler_scrape_completed",
            url=TOUR_URL,
            record_count=len(records),
        )
        return records

    @staticmethod
    def _parse_link(text: str, url: str, year: int) -> dict | None:
        match = re.fullmatch(
            r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3,9})\s+-\s+"
            r"(?P<place>.+?)\s+-\s+(?P<venue>.+)",
            text,
        )
        if not match:
            return None

        place = re.sub(r"\s+UK$", "", match.group("place"), flags=re.IGNORECASE).strip()
        venue = match.group("venue").strip()
        correction = LOCATION_CORRECTIONS.get((place, venue))
        if correction:
            city, country_code = correction
        else:
            city = place
            country_code = COUNTRIES_BY_CITY.get(city)
        if not city or not venue or not country_code:
            return None

        try:
            event_date = datetime.strptime(
                f"{match.group('day')} {match.group('month')} {year}", "%d %b %Y"
            ).date().isoformat()
        except ValueError:
            return None

        return {
            "title": SOURCE,
            "date": event_date,
            "url": url,
            "time_from": None,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": None,
        }


def main():
    ReinhardtBuhrCrawler().run()


if __name__ == "__main__":
    main()
