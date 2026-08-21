import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.kozena.cz/"
SOURCE = "Magdalena Kožená"
CALENDAR_URL = urljoin(SOURCE_URL, "en/calendar/all/list")

COUNTRY_CODES = {
    "Argentina": "AR",
    "Australia": "AU",
    "Austria": "AT",
    "Belgium": "BE",
    "Brasil": "BR",
    "Canada": "CA",
    "Chile": "CL",
    "Colombia": "CO",
    "Czech Republic": "CZ",
    "Denmark": "DK",
    "Finland": "FI",
    "France": "FR",
    "Germany": "DE",
    "Great Britain": "GB",
    "Greece": "GR",
    "Hong Kong Special Administrative Region of the People's Republic of China": "HK",
    "Hungary": "HU",
    "Ireland": "IE",
    "Italy": "IT",
    "Luxembourg": "LU",
    "Netherlands": "NL",
    "Norway": "NO",
    "People's Republic of China": "CN",
    "Poland": "PL",
    "Portugal": "PT",
    "Republic of Korea": "KR",
    "Romania": "RO",
    "Russia": "RU",
    "Slovakia": "SK",
    "Slovenia": "SI",
    "Spain": "ES",
    "Sweden": "SE",
    "Switzerland": "CH",
    "Turkey": "TR",
    "USA": "US",
}

# The source stores this venue at the city level of its location taxonomy.
VENUE_CITY_DEFAULTS = {
    ("USA", "Park Avenue Armory"): "New York",
}


class KozenaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="kozena_cz",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ClassicalBot/1.0"})

    def _get_soup(self, url, params=None):
        log_message("Fetching calendar page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    @staticmethod
    def _location_taxonomy(soup):
        venue_locations = {}
        cities = set()
        country = None
        city = None
        for option in soup.select('select[name="category[]"] option[value]'):
            label = option.get_text(" ", strip=True)
            if not option.get("value") or not label or label == "----":
                continue
            depth = 2 if label.startswith("-- --") else 1 if label.startswith("--") else 0
            name = re.sub(r"^(?:--\s*)+", "", label).strip()
            if depth == 0:
                country, city = name, None
            elif depth == 1:
                city = name
                cities.add((country, city))
            elif country and city:
                venue_locations[(country, name)] = city
        return venue_locations, cities

    def _listing_urls(self, calendar_id, venue_locations, cities):
        today = date.today()
        params = {
            "sd": "01",
            "sm": "01",
            "sy": "2000",
            "ed": "31",
            "em": "12",
            "ey": str(today.year + 3),
            "calendar": str(calendar_id),
        }
        page_url = CALENDAR_URL
        seen_pages = set()
        event_urls = []
        while page_url not in seen_pages:
            seen_pages.add(page_url)
            soup = self._get_soup(page_url, params=params)
            if not venue_locations:
                venues, known_cities = self._location_taxonomy(soup)
                venue_locations.update(venues)
                cities.update(known_cities)
            event_urls.extend(
                urljoin(SOURCE_URL, anchor["href"])
                for anchor in soup.select('a[href*="/calendar/"][href*="event-"]')
            )
            page_links = soup.select('a[href*="/list+"]')
            unvisited = [
                urljoin(SOURCE_URL, link["href"])
                for link in page_links
                if urljoin(SOURCE_URL, link["href"]) not in seen_pages
            ]
            page_url = unvisited[0] if unvisited else None
            params = None
            if not page_url:
                break
        return event_urls

    @staticmethod
    def _parse_datetime(text):
        normalized = re.sub(r"\s+", " ", text).strip()
        match = re.search(
            r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})(?:,\s*(\d{1,2}:\d{2}))?",
            normalized,
        )
        if not match:
            return None, None
        event_date = datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
        return event_date, match.group(2)

    def _parse_event(self, url, venue_locations, cities):
        soup = self._get_soup(url)
        event = soup.select_one(".element.event.full")
        if not event:
            return None

        title_node = event.select_one("h1.head")
        date_node = event.select_one(".hd.main .pale-color")
        location = [
            anchor.get_text(" ", strip=True)
            for anchor in event.select(".category-event .bd a")
            if anchor.get_text(" ", strip=True)
        ]
        event_date, time_from = self._parse_datetime(date_node.get_text(" ", strip=True) if date_node else "")
        if not title_node or not event_date or len(location) < 2:
            return None

        country = location[0]
        country_code = COUNTRY_CODES.get(country)
        if not country_code:
            return None

        if len(location) >= 3:
            city, venue = location[-2], location[-1]
        else:
            second = location[1]
            inferred_city = venue_locations.get((country, second)) or VENUE_CITY_DEFAULTS.get(
                (country, second)
            )
            if inferred_city:
                city, venue = inferred_city, second
            elif (country, second) in cities:
                return None
            else:
                return None

        description_node = event.select_one(".description")
        description = description_node.get_text("\n", strip=True) if description_node else None
        return {
            "title": title_node.get_text(" ", strip=True),
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description or None,
        }

    def scrape(self):
        venue_locations = {}
        cities = set()
        urls = []
        for calendar_id in (2, 3):
            urls.extend(self._listing_urls(calendar_id, venue_locations, cities))

        records = []
        for url in dict.fromkeys(urls):
            try:
                record = self._parse_event(url, venue_locations, cities)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch concert detail",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message("Concerts parsed", event="crawler_records_parsed", record_count=len(records))
        return records


def main():
    KozenaCrawler().run()


if __name__ == "__main__":
    main()
