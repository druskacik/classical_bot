import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.dejanlazic.com/"
ARCHIVE_URL = "https://www.dejanlazic.com/concert_pt/"
SOURCE = "Dejan Lazić"

# The artist tours internationally.  The site uses a mixture of ISO alpha-2,
# ISO alpha-3, and older vehicle registration codes in parentheses.
COUNTRY_CODES = {
    "A": "AT",
    "AUS": "AU",
    "B": "BE",
    "BR": "BR",
    "C": "CU",
    "CDN": "CA",
    "CH": "CH",
    "CN": "CN",
    "CZ": "CZ",
    "D": "DE",
    "DK": "DK",
    "E": "ES",
    "F": "FR",
    "GB": "GB",
    "GR": "GR",
    "H": "HU",
    "HR": "HR",
    "I": "IT",
    "IL": "IL",
    "J": "JP",
    "KOR": "KR",
    "L": "LU",
    "N": "NO",
    "NL": "NL",
    "NZ": "NZ",
    "P": "PT",
    "PL": "PL",
    "RO": "RO",
    "S": "SE",
    "SK": "SK",
    "SLO": "SI",
    "UK": "GB",
    "USA": "US",
}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _parse_location(location: Tag) -> tuple[str, str, str] | None:
    parts = [_clean_text(part) for part in location.stripped_strings]
    if len(parts) < 2:
        return None

    match = re.fullmatch(r"(.+?)\s*\(([A-Za-z]{1,3})\)", parts[0])
    if not match:
        return None

    city = match.group(1).strip().rstrip(",")
    venue = " ".join(parts[1:]).strip()
    country_code = COUNTRY_CODES.get(match.group(2).upper())
    if not city or not venue or not country_code:
        return None
    return city, venue, country_code


def _next_tag(element: Tag) -> Tag | None:
    sibling = element.next_sibling
    while sibling is not None:
        if isinstance(sibling, Tag):
            return sibling
        sibling = sibling.next_sibling
    return None


class DejanLazicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="dejanlazic_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert archive", event="crawler_url_fetch", url=ARCHIVE_URL)
        response = requests.get(ARCHIVE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        for heading in soup.select("h3.post-content__title"):
            link = heading.find("a", href=True)
            date_tag = _next_tag(heading)
            location_tag = _next_tag(date_tag) if date_tag else None
            description_tag = _next_tag(location_tag) if location_tag else None
            if not link or not date_tag or not location_tag:
                continue

            try:
                event_date = datetime.strptime(
                    _clean_text(date_tag.get_text(" ", strip=True)), "%b %d, %Y"
                ).date().isoformat()
            except ValueError:
                continue

            location = _parse_location(location_tag)
            if location is None:
                log_message(
                    "Skipping concert with unparseable location",
                    event="crawler_record_skipped",
                    url=link["href"],
                )
                continue
            city, venue, country_code = location

            title = _clean_text(link.get_text(" ", strip=True))
            url = link["href"].strip()
            description = None
            if description_tag and description_tag.name == "p":
                description = _clean_text(description_tag.get_text(" ", strip=True)) or None
            if not title or not url:
                continue

            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                }
            )

        log_message(
            "Concert archive parsed",
            event="crawler_scrape_completed",
            url=ARCHIVE_URL,
            record_count=len(records),
        )
        return records


def main():
    DejanLazicCrawler().run()


if __name__ == "__main__":
    main()
