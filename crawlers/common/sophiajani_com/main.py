from datetime import date
import json

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.sophiajani.com/"
SOURCE = "Sophia Jani"

# The site mostly uses informal three-letter abbreviations rather than ISO
# 3166-1 alpha-2 codes. Both DE and GER occur in the source data.
COUNTRY_CODES = {
    "AUT": "AT",
    "CAN": "CA",
    "CUB": "CU",
    "DE": "DE",
    "DEN": "DK",
    "GER": "DE",
    "ITA": "IT",
    "UK": "GB",
    "USA": "US",
}


def normalize_country_code(raw_code, city):
    code = (raw_code or "").strip().upper()
    city = city.strip()

    # AUS is inconsistently used for Australia, Austria, and one US event.
    if code == "AUS":
        if city == "Brisbane":
            return "AU"
        if city == "Salzburg":
            return "AT"
        if city == "Princeton, NJ":
            return "US"
        return None

    return COUNTRY_CODES.get(code)


def parse_concerts(html):
    soup = BeautifulSoup(html, "html.parser")
    data_element = soup.find("script", id="__NEXT_DATA__")
    if data_element is None or not data_element.string:
        raise ValueError("Page does not contain embedded Next.js concert data")

    page_data = json.loads(data_element.string)
    concerts = page_data["props"]["pageProps"]["concerts"]
    records = []

    for concert in concerts:
        title = (concert.get("info") or "").strip()
        city = (concert.get("location") or "").strip()
        venue = (concert.get("venue") or "").strip()
        raw_date = str(concert.get("date") or "").strip()
        event_url = str(concert.get("link") or "").strip() or SOURCE_URL
        country_code = normalize_country_code(concert.get("countryCode"), city)

        try:
            concert_date = date.fromisoformat(raw_date).isoformat()
        except (TypeError, ValueError):
            concert_date = None

        if not all((title, concert_date, city, venue, country_code)):
            log_message(
                "Skipping incomplete concert",
                event="crawler_record_skipped",
                record_id=concert.get("_id"),
                error_type="IncompleteConcertData",
            )
            continue

        records.append(
            {
                "title": title,
                "date": concert_date,
                "url": event_url,
                "time_from": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": title,
            }
        )

    return records


class SophiaJaniCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sophiajani_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "venue", "city", "country_code"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching concert catalogue", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        return parse_concerts(response.text)


def main():
    SophiaJaniCrawler().run()


if __name__ == "__main__":
    main()
