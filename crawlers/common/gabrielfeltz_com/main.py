import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Gabriel Feltz"
SOURCE_URL = "https://www.gabrielfeltz.com/"
CALENDAR_URL = f"{SOURCE_URL}calendar"

# Gabriel Feltz's calendar is an international touring calendar.  The site
# supplies a venue/location string but no country field, so resolve the cities
# represented in the feed explicitly rather than assigning his home country to
# every performance.
LOCATION_GEOGRAPHY = {
    "belgrade": ("Belgrade", "RS"),
    "dortmund": ("Dortmund", "DE"),
    "kiel": ("Kiel", "DE"),
    "milano": ("Milan", "IT"),
    "novi sad": ("Novi Sad", "RS"),
    "osaka": ("Osaka", "JP"),
    "prague": ("Prague", "CZ"),
    "siegen": ("Siegen", "DE"),
}


def clean_text(value: str) -> str:
    value = value.replace("\u200d", "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_location(value: str) -> tuple[str, str, str] | None:
    location = clean_text(value)
    lower_location = location.casefold()
    for marker, (city, country_code) in LOCATION_GEOGRAPHY.items():
        if marker in lower_location:
            # The calendar uses the whole location as its venue label.  This
            # retains useful hall names without leaking them into the city.
            return location, city, country_code
    return None


class GabrielFeltzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="gabrielfeltz_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city", "country_code"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(
            CALENDAR_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        skipped_count = 0
        for item in soup.select(".collection-item-2.w-dyn-item"):
            title_node = item.select_one("h3")
            date_time_nodes = item.select(".div-block-17 > div")
            location_node = item.select_one(".text-block-2")
            url_node = item.select_one("a.btn-small[href]")

            if not title_node or len(date_time_nodes) < 3 or not location_node or not url_node:
                skipped_count += 1
                continue

            location = parse_location(location_node.get_text(" ", strip=True))
            if location is None:
                skipped_count += 1
                log_message(
                    "Skipping event with unresolved location",
                    event="crawler_record_skipped",
                    url=url_node.get("href"),
                    error_type="UnresolvedLocation",
                )
                continue
            venue, city, country_code = location

            try:
                event_date = datetime.strptime(
                    date_time_nodes[0].get_text(strip=True), "%d.%m.%Y"
                ).date().isoformat()
            except ValueError:
                skipped_count += 1
                continue

            time_text = date_time_nodes[2].get_text(strip=True)
            time_from = time_text if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_text) else None
            description_node = item.select_one(".rich-text-block-2")
            description = clean_text(description_node.get_text("\n")) if description_node else None

            records.append(
                {
                    "title": clean_text(title_node.get_text(" ", strip=True)),
                    "date": event_date,
                    "url": url_node["href"],
                    "time_from": time_from,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description or None,
                }
            )

        log_message(
            "Calendar parsed",
            event="crawler_parse_completed",
            record_count=len(records),
            skipped_count=skipped_count,
            url=CALENDAR_URL,
        )
        return records


def main():
    GabrielFeltzCrawler().run()


if __name__ == "__main__":
    main()
