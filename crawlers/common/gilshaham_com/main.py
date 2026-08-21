import json
import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Gil Shaham"
SOURCE_URL = "https://gilshaham.com/"
SCHEDULE_URL = "https://gilshaham.com/schedule/"

COUNTRY_CODES = {
    "Australia": "AU",
    "Canada": "CA",
    "China": "CN",
    "Germany": "DE",
    "Israel": "IL",
    "Italy": "IT",
    "Mexico": "MX",
    "Monaco": "MC",
    "New Zealand": "NZ",
    "South Korea": "KR",
    "Switzerland": "CH",
    "USA": "US",
}


def _clean_text(value):
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", unescape(value)).strip()
    return cleaned or None


def _clean_city(city, country):
    city = _clean_text(city)
    if not city:
        return None
    if country in {"USA", "Canada"}:
        city = re.sub(r"\s+[A-Z]{2}$", "", city)
    if country == "China":
        city = re.sub(r"\s+Guangdong Province$", "", city)
    if country == "USA" and city == "New York City":
        city = "New York"
    return city


def _parse_event(data):
    location = data.get("location") or {}
    address = location.get("address") or {}
    country = _clean_text(address.get("addressCountry"))
    country_code = COUNTRY_CODES.get(country)
    if not country_code:
        raise ValueError(f"Unsupported country: {country!r}")

    start = datetime.fromisoformat(data["startDate"])
    title = _clean_text(data.get("name"))
    venue = _clean_text(location.get("name"))
    city = _clean_city(address.get("addressLocality"), country)
    url = _clean_text(data.get("url"))
    if not all((title, venue, city, url)):
        raise ValueError("Event is missing a required title, venue, city, or URL")

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": start.time().replace(tzinfo=None).isoformat(),
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": _clean_text(data.get("description")),
    }


class GilShahamCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="gilshaham_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "time_from", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching schedule", event="crawler_url_fetch", url=SCHEDULE_URL)
        response = requests.get(SCHEDULE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        scripts = soup.select('.e-loop-item script[type="application/ld+json"]')
        for script in scripts:
            try:
                data = json.loads(script.get_text())
                if data.get("@type") == "Event":
                    records.append(_parse_event(data))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                log_message(
                    "Skipping invalid schedule event",
                    event="crawler_parse_error",
                    url=SCHEDULE_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        log_message(
            "Schedule parsed",
            event="crawler_page_parsed",
            url=SCHEDULE_URL,
            record_count=len(records),
        )
        return records


def main():
    GilShahamCrawler().run()


if __name__ == "__main__":
    main()
