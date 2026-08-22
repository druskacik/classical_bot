from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://soltango.com/"
CONCERTS_URL = urljoin(SOURCE_URL, "concerts/")
SOURCE = "Cuarteto SolTango"

MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}

# The ensemble is based in Germany, but its calendar also contains tour dates.
# These are the non-German cities currently represented in the archive.
COUNTRY_BY_CITY = {
    "Lenzburg": "CH",
    "Linz": "AT",
    "Sarıyer/İstanbul": "TR",
    "Tirolo BZ": "IT",
}


def _clean(value):
    return " ".join(value.split()).strip(' "')


def _parse_place(value):
    value = " ".join(value.split()).strip()
    if value.endswith(",") and value.startswith('"'):
        # One archived entry is formatted as "Schwerin, Rittersaal (...) ",
        # with the city first and an empty city field after the closing quote.
        city, venue = value.rstrip(",").strip(' "').split(",", 1)
        return _clean(venue), _clean(city)

    venue, separator, city = value.rpartition(",")
    if not separator:
        return None, None
    return _clean(venue), _clean(city)


def _event_url(cell):
    links = cell.select("a[href]")
    for label in ("website", "tickets"):
        for link in links:
            if link.get_text(" ", strip=True).lower() == label:
                return urljoin(CONCERTS_URL, link["href"])
    return CONCERTS_URL


class SoltangoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="soltango_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="DE",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching concert archive", event="crawler_url_fetch", url=CONCERTS_URL)
        response = requests.get(
            CONCERTS_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for grid in soup.select(".x-grid"):
            cells = grid.select(":scope > .x-cell")
            if len(cells) != 4:
                continue

            date_parts = list(cells[0].stripped_strings)
            if len(date_parts) < 4 or date_parts[1] not in MONTHS:
                continue
            try:
                event_date = datetime(
                    int(date_parts[2]), MONTHS[date_parts[1]], int(date_parts[0])
                ).date()
            except ValueError:
                continue

            title_parts = [_clean(part) for part in cells[1].stripped_strings]
            place_parts = list(cells[3].stripped_strings)
            if not title_parts or not place_parts:
                continue
            venue, city = _parse_place(place_parts[-1])
            if not venue or not city:
                continue

            time_from = next(
                (part.removesuffix("h") for part in date_parts[3:] if part.endswith("h")),
                None,
            )
            description = "\n".join(title_parts[1:]) or None
            records.append(
                {
                    "title": title_parts[0],
                    "date": event_date.isoformat(),
                    "url": _event_url(cells[2]),
                    "time_from": time_from,
                    "venue": venue,
                    "city": city,
                    "country_code": COUNTRY_BY_CITY.get(city, "DE"),
                    "description": description,
                }
            )

        log_message(
            "Concert archive parsed",
            event="crawler_scrape_completed",
            url=CONCERTS_URL,
            record_count=len(records),
        )
        return records


def main():
    SoltangoCrawler().run()


if __name__ == "__main__":
    main()
