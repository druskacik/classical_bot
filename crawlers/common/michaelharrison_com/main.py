from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Michael Harrison"
SOURCE_URL = "https://www.michaelharrison.com/"
EVENTS_URL = f"{SOURCE_URL}events"


def _country_code(location: str) -> str | None:
    """Infer country only from the explicit region/country shown by the site."""
    tail = location.rsplit("—", 1)[-1].strip()
    region = tail.rsplit(",", 1)[-1].strip()
    if region in {"England", "United Kingdom"}:
        return "GB"
    if region == "France":
        return "FR"
    if region in {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    }:
        return "US"
    return None


def _parse_location(location: str) -> tuple[str, str] | None:
    if "—" not in location:
        return None
    venue, place = (part.strip() for part in location.split("—", 1))
    city = place.split(",", 1)[0].strip()
    if not venue or not city:
        return None
    return venue, city


class MichaelHarrisonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="michaelharrison_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching events page", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(EVENTS_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []

        for item in soup.select("article.event-item"):
            date_node = item.select_one(".event-date")
            title_node = item.select_one(".event-title")
            location_node = item.select_one(".event-location")
            if not date_node or not title_node or not location_node:
                continue

            try:
                event_date = datetime.strptime(
                    date_node.get_text(" ", strip=True), "%B %d, %Y"
                ).date().isoformat()
            except ValueError:
                # The archive includes year-only highlights, which are not
                # concrete occurrences and cannot produce a valid date.
                continue

            location = " ".join(location_node.stripped_strings)
            parsed_location = _parse_location(location)
            country_code = _country_code(location)
            if parsed_location is None or country_code is None:
                continue
            venue, city = parsed_location

            description_node = item.select_one(".event-description")
            description = (
                description_node.get_text(" ", strip=True)
                if description_node
                else None
            )
            records.append(
                {
                    "title": title_node.get_text(" ", strip=True),
                    "date": event_date,
                    "url": EVENTS_URL,
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                }
            )

        log_message(
            "Parsed events page",
            event="crawler_scrape_parsed",
            url=EVENTS_URL,
            record_count=len(records),
        )
        return records


def main():
    MichaelHarrisonCrawler().run()


if __name__ == "__main__":
    main()
