import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "La Venexiana"
SOURCE_URL = "https://www.lavenexiana.net/"
ARCHIVE_URL = urljoin(SOURCE_URL, "events/past/")
TIMEOUT = 30

# La Venexiana is based in Italy but tours internationally. These aliases cover
# the locations published in its retained event archive; unknown locations are
# deliberately skipped rather than assigned the ensemble's home country.
LOCATIONS = {
    "potsdam": ("Potsdam", "DE"),
    "katowice": ("Katowice", "PL"),
    "bologna": ("Bologna", "IT"),
    "berlin": ("Berlin", "DE"),
    "tallin": ("Tallinn", "EE"),
    "parnu": ("Pärnu", "EE"),
    "pirano": ("Piran", "SI"),
    "pisa": ("Pisa", "IT"),
    "bonn": ("Bonn", "DE"),
    "koln": ("Cologne", "DE"),
    "sassari": ("Sassari", "IT"),
    "forli": ("Forlì", "IT"),
    "brunico": ("Brunico", "IT"),
    "bolzano": ("Bolzano", "IT"),
    "bellelay": ("Bellelay", "CH"),
    "londra": ("London", "GB"),
    "london": ("London", "GB"),
}

VENUE_WORDS = re.compile(
    r"\b(theater|theatre|teatro|saal|hall|chiesa|kirche|abbazia|abbaye|"
    r"auditorium|museum|chiostro|convento|cortile)\b",
    re.IGNORECASE,
)


def clean_text(value):
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def location_for(text):
    normalized = (
        unescape(text).lower().replace("ä", "a").replace("ö", "o")
        .replace("ü", "u").replace("ì", "i").replace("ù", "u")
    )
    for alias, result in LOCATIONS.items():
        if alias in normalized:
            return result
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1")
    start_node = soup.select_one('.event_short_details [itemprop="startDate"]')
    location_node = soup.select_one('.event_short_details [itemprop="location"]')
    if not title_node or not start_node or not location_node:
        return None

    start_value = start_node.get("content", "")
    try:
        start = datetime.fromisoformat(start_value)
    except ValueError:
        return None
    clock_icon = soup.select_one(".event_short_details .fa-clock-o")
    time_from = None
    if clock_icon:
        clock_text = clean_text(clock_icon.parent.get_text(" ", strip=True))
        match = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", clock_text)
        if match:
            time_from = match.group(0)

    address_node = location_node.select_one('[itemprop="address"]')
    name_node = location_node.select_one('[itemprop="name"]')
    address = clean_text(address_node.get_text(" ", strip=True) if address_node else "")
    place_name = clean_text(name_node.get_text(" ", strip=True) if name_node else "")
    resolved = location_for(" ".join((address, place_name)))
    if not resolved:
        return None
    city, country_code = resolved

    if VENUE_WORDS.search(address):
        venue = address
    elif place_name:
        venue = place_name
    elif "," in address:
        venue = clean_text(address.split(",", 1)[1])
    else:
        return None
    # Some address fields combine a city/address and a venue. Retain only the
    # venue name so postal details do not leak into the venue column.
    venue = venue.split("|", 1)[0].strip()
    if "," in venue and location_for(venue.split(",", 1)[0]):
        venue = venue.split(",", 1)[1].strip()
    venue = re.sub(r"\s*\([^)]*(?:estonia|italy|italia|de|it|ch)\)\s*$", "", venue, flags=re.I)
    if not venue or venue.casefold() == city.casefold():
        return None

    content = soup.select_one(".swp_event_content")
    description = clean_text(content.get_text("\n", strip=True)) if content else None
    return {
        "title": clean_text(title_node.get_text(" ", strip=True)),
        "date": start.date().isoformat(),
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description or None,
    }


class LaVenexianaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="lavenexiana_net",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers["User-Agent"] = "classical-bot concert crawler"
        log_message("Fetching event archive", event="crawler_url_fetch", url=ARCHIVE_URL)
        response = session.get(ARCHIVE_URL, timeout=TIMEOUT)
        response.raise_for_status()
        archive = BeautifulSoup(response.text, "html.parser")
        urls = list(dict.fromkeys(
            urljoin(ARCHIVE_URL, anchor["href"])
            for anchor in archive.select('.single_event_list a[href*="/event/"]')
        ))

        records = []
        for url in urls:
            try:
                log_message("Fetching event detail", event="crawler_url_fetch", url=url)
                detail = session.get(url, timeout=TIMEOUT)
                detail.raise_for_status()
                record = parse_detail(detail.text, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    "Event detail fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        log_message(
            "Event archive parsed",
            event="crawler_scrape_completed",
            url=ARCHIVE_URL,
            record_count=len(records),
        )
        return records


def main():
    LaVenexianaCrawler().run()


if __name__ == "__main__":
    main()
