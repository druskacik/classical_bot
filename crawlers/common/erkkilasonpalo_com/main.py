import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://erkkilasonpalo.com/"
SOURCE = "Erkki Lasonpalo"
EVENTS_URL = urljoin(SOURCE_URL, "events")
REQUEST_TIMEOUT = 30

# The site's field labelled as a city contains a venue on the usable archive
# records.  These mappings are deliberately explicit: many other cards omit a
# venue altogether, and an orchestra or a city must not be used as a venue.
VENUE_LOCATIONS = {
    "finnish national opera and ballet": ("Finnish National Opera and Ballet", "Helsinki", "FI"),
    "keski-porin kirkko": ("Keski-Porin kirkko", "Pori", "FI"),
    "kuopion musiikkikeskus": ("Kuopion musiikkikeskus", "Kuopio", "FI"),
    "lappeenranta-sali": ("Lappeenranta-sali", "Lappeenranta", "FI"),
    "lappeenranta- sali": ("Lappeenranta-sali", "Lappeenranta", "FI"),
    "martti talvela": ("Martti Talvela -sali", "Mikkeli", "FI"),
    "martti talvela-sali": ("Martti Talvela -sali", "Mikkeli", "FI"),
    "metro areena": ("Metro Areena", "Espoo", "FI"),
    "mikkelin tuomiokirkko": ("Mikkelin tuomiokirkko", "Mikkeli", "FI"),
    "nokia arena": ("Nokia Arena", "Tampere", "FI"),
    "savonlinnan tuomiokirkko": ("Savonlinnan tuomiokirkko", "Savonlinna", "FI"),
    "sibeliustalo": ("Sibeliustalo", "Lahti", "FI"),
}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _parse_date(value: str) -> str | None:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


class ErkkiLasonpaloCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="erkkilasonpalo_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def _get_soup(self, url: str) -> BeautifulSoup:
        log_message("Fetching event listing", event="crawler_url_fetch", url=url)
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def scrape(self) -> list[dict]:
        first_page = self._get_soup(EVENTS_URL)
        page_numbers = [
            int(link["data-ci-pagination-page"])
            for link in first_page.select("a[data-ci-pagination-page]")
            if link.get("data-ci-pagination-page", "").isdigit()
        ]
        last_page = max(page_numbers, default=1)
        records = []

        for page_number in range(1, last_page + 1):
            page_url = EVENTS_URL if page_number == 1 else f"{EVENTS_URL}?page={page_number}"
            soup = first_page if page_number == 1 else self._get_soup(page_url)

            for card in soup.select(".events_itm"):
                title_node = card.select_one(".events_itm_text")
                date_node = card.select_one(".events_itm_date")
                location_node = card.select_one(".events_itm_city")
                title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
                date = _parse_date(date_node.get_text(strip=True)) if date_node else None
                location_key = _clean_text(location_node.get_text(" ", strip=True)).casefold() if location_node else ""
                location = VENUE_LOCATIONS.get(location_key)

                if not title or not date or not location:
                    log_message(
                        "Skipping event without a complete date and venue location",
                        event="crawler_event_skipped",
                        url=page_url,
                        has_title=bool(title),
                        has_date=bool(date),
                        has_venue_location=bool(location),
                    )
                    continue

                venue, city, country_code = location
                link = card.select_one("a.events_itm_link")
                href = link.get("href") if link else None
                event_url = urljoin(SOURCE_URL, href) if href and href != "#" else page_url
                records.append(
                    {
                        "title": title,
                        "date": date,
                        "url": event_url,
                        "time_from": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": None,
                    }
                )

        return records


def main():
    ErkkiLasonpaloCrawler().run()


if __name__ == "__main__":
    main()
