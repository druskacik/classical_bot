import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.richard-danielpour.com/"
SOURCE = "Richard Danielpour"
DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"([A-Za-z]+),?\s+(\d{1,2}),?\s+(\d{4})\s+at\s+"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))",
    re.IGNORECASE,
)
US_LOCATION_RE = re.compile(
    r"^(?P<prefix>.+),\s*(?P<city>[^,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?$"
)
COUNTRY_CODES = {
    "australia": "AU",
    "austria": "AT",
    "canada": "CA",
    "france": "FR",
    "germany": "DE",
    "italy": "IT",
    "netherlands": "NL",
    "spain": "ES",
    "switzerland": "CH",
    "united kingdom": "GB",
    "uk": "GB",
    "united states": "US",
    "usa": "US",
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u200b", " ").replace("\ufeff", " ")).strip()


def _venue_from_prefix(prefix: str) -> str | None:
    parts = [part.strip() for part in prefix.split(",") if part.strip()]
    address_index = next((i for i, part in enumerate(parts) if re.search(r"\d", part)), len(parts))
    venue_parts = parts[:address_index]
    return ", ".join(venue_parts) or None


def _parse_location(value: str) -> tuple[str, str, str] | None:
    location = _clean(value)
    us_match = US_LOCATION_RE.match(location)
    if us_match:
        venue = _venue_from_prefix(us_match.group("prefix"))
        city = us_match.group("city").strip()
        # Some Wix copy omits the comma between the street address and city.
        if city[:1].isdigit():
            city_match = re.search(
                r"\b(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.?)\s+(.+)$",
                city,
                re.IGNORECASE,
            )
            city = city_match.group(1).strip() if city_match else ""
        if venue:
            if city:
                return venue, city, "US"

    parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(parts) >= 3:
        country_code = COUNTRY_CODES.get(parts[-1].lower())
        if country_code:
            venue = _venue_from_prefix(", ".join(parts[:-2]))
            if venue:
                return venue, parts[-2], country_code
    return None


def _event_blocks(soup: BeautifulSoup) -> list:
    blocks = []
    for div in soup.select("div.gpDCD5"):
        text = _clean(div.get_text(" ", strip=True))
        if DATE_RE.search(text):
            blocks.append(div)
    return blocks


def _parse_block(block) -> list[dict]:
    strings = [_clean(value) for value in block.stripped_strings]
    strings = [value for value in strings if value]
    dated_indexes = [index for index, value in enumerate(strings) if DATE_RE.search(value)]
    if not dated_indexes:
        return []

    title_index = dated_indexes[0] - 1
    if title_index < 0:
        return []
    title = strings[title_index]

    location_index = dated_indexes[-1] + 1
    if location_index >= len(strings):
        return []
    location = _parse_location(strings[location_index])
    if not location:
        return []
    venue, city, country_code = location

    description_parts = strings[location_index + 1 :]
    description = "\n".join(description_parts) or None
    records = []
    for index in dated_indexes:
        match = DATE_RE.search(strings[index])
        if not match:
            continue
        month, day, year, raw_time = match.groups()
        try:
            event_date = datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date().isoformat()
            time_from = datetime.strptime(raw_time.replace(" ", "").upper(), "%I:%M%p").time()
        except ValueError:
            try:
                time_from = datetime.strptime(raw_time.replace(" ", "").upper(), "%I%p").time()
            except ValueError:
                continue
        records.append(
            {
                "title": title,
                "date": event_date,
                "url": SOURCE_URL,
                "time_from": time_from.strftime("%H:%M"),
                "time_to": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description,
            }
        )
    return records


class RichardDanielpourCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="richard_danielpour_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert listings", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for block in _event_blocks(soup):
            records.extend(_parse_block(block))
        log_message("Concert listings parsed", event="crawler_parse_completed", record_count=len(records))
        return records


def main():
    RichardDanielpourCrawler().run()


if __name__ == "__main__":
    main()
