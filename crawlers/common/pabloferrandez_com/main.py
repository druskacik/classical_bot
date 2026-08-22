import re
import unicodedata
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Pablo Ferrández"
SOURCE_URL = "https://pabloferrandez.com/"
REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)",
}

# The artist tours internationally and the site writes the country name as the
# final component of each location. Keep this explicit so an unknown location
# is skipped rather than assigned to the artist's home country.
COUNTRY_CODES = {
    "argentina": "AR", "australia": "AU", "austria": "AT",
    "belgium": "BE", "brazil": "BR", "canada": "CA", "chile": "CL",
    "china": "CN", "colombia": "CO", "croatia": "HR",
    "czech republic": "CZ", "czechia": "CZ", "denmark": "DK",
    "estonia": "EE", "finland": "FI", "france": "FR", "germany": "DE",
    "greece": "GR", "hong kong": "HK", "hungary": "HU",
    "iceland": "IS", "ireland": "IE", "israel": "IL", "italy": "IT",
    "japan": "JP", "latvia": "LV", "lithuania": "LT",
    "luxembourg": "LU", "mexico": "MX", "monaco": "MC",
    "netherlands": "NL", "new zealand": "NZ", "norway": "NO",
    "poland": "PL", "portugal": "PT", "romania": "RO",
    "singapore": "SG", "slovakia": "SK", "slovenia": "SI",
    "south korea": "KR", "spain": "ES", "sweden": "SE",
    "switzerland": "CH", "taiwan": "TW", "turkey": "TR",
    "united arab emirates": "AE", "united kingdom": "GB", "uk": "GB",
    "united states": "US", "united states of america": "US", "usa": "US",
}


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text or None


def normalize_country(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold().strip(" .")


class PabloFerrandezCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="pabloferrandez_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, headers=REQUEST_HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for item in soup.select("#myConcerts li.perform-tile"):
            record = self._parse_event(item)
            if record is not None:
                records.append(record)

        log_message("Concerts parsed", event="crawler_records_parsed", record_count=len(records))
        return records

    @staticmethod
    def _parse_event(item) -> dict | None:
        title = clean_text(item.select_one(".gig-title").get_text(" ", strip=True)) if item.select_one(".gig-title") else None
        venue = clean_text(item.select_one(".gig-venue").get_text(" ", strip=True)) if item.select_one(".gig-venue") else None
        location = clean_text(item.select_one(".gig-location").get_text(" ", strip=True)) if item.select_one(".gig-location") else None
        date_text = clean_text(item.select_one(".gig-dates-list").get_text(" ", strip=True)) if item.select_one(".gig-dates-list") else None
        link = item.select_one(".gig-more a[href]")
        url = urljoin(SOURCE_URL, link.get("href", "").strip()) if link else None

        city = country_code = None
        if location and "," in location:
            city_part, country_part = location.rsplit(",", 1)
            city = clean_text(city_part)
            country_code = COUNTRY_CODES.get(normalize_country(country_part))

        event_date = None
        if date_text:
            try:
                event_date = datetime.strptime(date_text, "%B %d,%Y").date().isoformat()
            except ValueError:
                pass

        if not all((title, venue, city, country_code, event_date, url)):
            log_message(
                "Skipping incomplete concert",
                event="crawler_record_skipped",
                url=url,
                location=location,
                date_text=date_text,
            )
            return None

        notes = [clean_text(node.get_text(" ", strip=True)) for node in item.select(".gig-notes")]
        description = "\n".join(note for note in notes if note) or None
        return {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": None,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }


def main():
    PabloFerrandezCrawler().run()


if __name__ == "__main__":
    main()
