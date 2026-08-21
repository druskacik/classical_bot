from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Herbie Hancock"
SOURCE_URL = "https://www.herbiehancock.com/"
TOUR_URL = urljoin(SOURCE_URL, "tour/")
PAST_TOUR_URL = urljoin(SOURCE_URL, "tour/past/")

US_REGIONS = {
    "AL", "AR", "AZ", "CA", "CO", "CT", "DC", "D.C", "FL", "GA",
    "IA", "IL", "IN", "KY", "LA", "MA", "MD", "ME", "MI", "MN",
    "MO", "NC", "NJ", "NV", "NY", "OH", "OK", "OR", "PA", "RI",
    "SC", "TN", "TX", "UT", "VA", "WA", "WI",
}
CANADIAN_REGIONS = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
COUNTRY_CODES = {
    "AU": "AU",
    "Belgium": "BE",
    "CN": "CN",
    "Denmark": "DK",
    "England": "GB",
    "ESP": "ES",
    "Finland": "FI",
    "FR": "FR",
    "France": "FR",
    "Frances": "FR",
    "Germany": "DE",
    "Greece": "GR",
    "Ireland": "IE",
    "Italy": "IT",
    "Japan": "JP",
    "JPN": "JP",
    "Netherlands": "NL",
    "New Zealand": "NZ",
    "NZ": "NZ",
    "Poland": "PL",
    "Scotland": "GB",
    "South Korea": "KR",
    "Spain": "ES",
    "Sweden": "SE",
    "Switzerland": "CH",
    "The Netherlands": "NL",
    "UK": "GB",
}


def clean_text(node):
    if node is None:
        return None
    value = " ".join(node.get_text(" ", strip=True).split())
    return value or None


def parse_location(value):
    parts = [part.strip() for part in value.rsplit(",", 1)]
    if len(parts) != 2 or not all(parts):
        return None

    city, region = parts
    if region in US_REGIONS:
        return city, "US"
    if region in CANADIAN_REGIONS:
        return city, "CA"
    country_code = COUNTRY_CODES.get(region)
    if country_code:
        return city, country_code
    return None


def parse_event(item, page_url):
    year = clean_text(item.select_one(".year"))
    month = clean_text(item.select_one(".month"))
    day = clean_text(item.select_one(".day"))
    venue = clean_text(item.select_one(".venue"))
    location = clean_text(item.select_one(".location"))
    if not all((year, month, day, venue, location)):
        return None

    try:
        event_date = datetime.strptime(f"{year} {month} {day}", "%Y %B %d").date().isoformat()
    except ValueError:
        return None

    resolved_location = parse_location(location)
    if resolved_location is None:
        return None
    city, country_code = resolved_location

    ticket_link = item.select_one(".url a[href]")
    event_url = urljoin(page_url, ticket_link["href"]) if ticket_link else page_url
    band = clean_text(item.select_one(".band"))
    note = clean_text(item.select_one(".note"))
    description_parts = [part for part in (band, note) if part]

    return {
        "title": f"Herbie Hancock at {venue}",
        "date": event_date,
        "url": event_url,
        "time_from": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": "\n".join(description_parts) or None,
    }


class HerbieHancockCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="herbiehancock_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city", "country_code"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        headers = {"User-Agent": "classical-concert-crawler/1.0"}
        for page_url, selector in (
            (TOUR_URL, ".itin-item"),
            (PAST_TOUR_URL, "article ul li"),
        ):
            log_message("Fetching tour page", event="crawler_url_fetch", url=page_url)
            response = requests.get(page_url, headers=headers, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            items = soup.select(selector)
            skipped_count = 0
            for item in items:
                record = parse_event(item, page_url)
                if record is None:
                    skipped_count += 1
                    continue
                records.append(record)
            log_message(
                "Tour page parsed",
                event="crawler_page_parsed",
                url=page_url,
                record_count=len(items) - skipped_count,
                skipped_count=skipped_count,
            )
        return records


def main():
    HerbieHancockCrawler().run()


if __name__ == "__main__":
    main()
