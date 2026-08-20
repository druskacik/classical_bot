import json
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "A. R. Rahman"
SOURCE_URL = "https://www.arrahman.com/"
PERFORMANCE_URL = "https://www.arrahman.com/performance"
CMS_URL = (
    "https://framerusercontent.com/cms/ZMiWeeT5NLO0VK0xKJ9c/"
    "OUeJ2oIymyqGVLhUVTSZ/WMC2TjAgC-chunk-default-0.framercms"
)

COUNTRIES_BY_CITY = {
    "abu dhabi": "AE",
    "bengaluru": "IN",
    "cincinnati": "US",
    "kallang": "SG",
    "kuala lumpur": "MY",
    "singapore": "SG",
}

FIELD_TITLE = "aN_2eORCS"
FIELD_SLUG = "USkkTzzid"
FIELD_START = "B8Ukxid3d"
FIELD_LOCATION = "X0U9PEucR"
FIELD_VENUE = "y7eAHbrl7"
FIELD_TICKET_URL = "syL9ZF1NC"
FIELD_ARCHIVE_DATE = "ADbEdu3ps"
FIELD_ARCHIVED = "kUMkFVACf"


def _cms_string(data: bytes, field: str) -> str | None:
    prefix = len(field).to_bytes(4, "big") + field.encode()
    for value_type in (b"\x0c", b"\x07"):
        key = prefix + value_type
        position = data.find(key)
        if position < 0:
            continue
        position += len(key)
        length = int.from_bytes(data[position : position + 4], "big")
        value = data[position + 4 : position + 4 + length]
        text = value.decode("utf-8", errors="replace").strip()
        if value_type == b"\x07":
            try:
                text = json.loads(text)
            except json.JSONDecodeError:
                pass
        return text or None
    return None


def _cms_milliseconds(data: bytes, field: str) -> int | None:
    key = len(field).to_bytes(4, "big") + field.encode() + b"\x04"
    position = data.find(key)
    if position < 0:
        return None
    position += len(key)
    return int.from_bytes(data[position : position + 8], "big")


def _cms_boolean(data: bytes, field: str) -> bool | None:
    key = len(field).to_bytes(4, "big") + field.encode() + b"\x02"
    position = data.find(key)
    if position < 0:
        return None
    return bool(data[position + len(key)])


def _country_code(location: str, venue: str) -> str | None:
    text = f"{location} {venue}".lower()
    for city, code in COUNTRIES_BY_CITY.items():
        if city in text:
            return code
    return None


def _clean_venue(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"^Venue:\s*", "", value, flags=re.IGNORECASE).strip() or None


def _records_from_cms(content: bytes) -> list[dict]:
    marker = len("id").to_bytes(4, "big") + b"id\x0c"
    chunks = content.split(marker)[1:]
    records = []

    for chunk in chunks:
        title = _cms_string(chunk, FIELD_TITLE)
        location = _cms_string(chunk, FIELD_LOCATION) or ""
        venue = _clean_venue(_cms_string(chunk, FIELD_VENUE))
        start_ms = _cms_milliseconds(chunk, FIELD_START)
        archive_date = _cms_string(chunk, FIELD_ARCHIVE_DATE)
        archived = _cms_boolean(chunk, FIELD_ARCHIVED)

        event_date = None
        time_from = None
        if start_ms and not archived:
            start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
            event_date = start.date().isoformat()
            time_from = start.strftime("%H:%M")
        elif archive_date and re.fullmatch(r"\d{1,2} [A-Za-z]{3}, \d{4}", archive_date):
            event_date = datetime.strptime(archive_date, "%d %b, %Y").date().isoformat()

        country_code = _country_code(location, venue or "")
        if not all((title, event_date, venue, location, country_code)):
            continue

        ticket_url = _cms_string(chunk, FIELD_TICKET_URL)
        description = "\n".join(part for part in (title, location, venue) if part)
        records.append(
            {
                "title": title,
                "date": event_date,
                "url": ticket_url or PERFORMANCE_URL,
                "time_from": time_from,
                "venue": venue,
                "city": location.split(",", 1)[0].strip(),
                "country_code": country_code,
                "description": description,
            }
        )
    return records


def _records_from_html(html: str) -> list[dict]:
    """Fallback for current events if Framer changes its CMS asset URL."""
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for time_tag in soup.find_all("time", datetime=True):
        card = time_tag
        while card.parent and "Venue:" not in card.get_text(" ", strip=True):
            card = card.parent
        while card.parent and not card.find("a", href=True):
            card = card.parent
        texts = [p.get_text(" ", strip=True) for p in card.find_all("p")]
        venue_text = next((text for text in texts if text.startswith("Venue:")), None)
        venue = _clean_venue(venue_text)
        title = next((text for text in texts if text and text != venue_text and "Get Tickets" not in text), None)
        start = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
        location = title.rsplit(",", 1)[-1].strip() if title and "," in title else ""
        country_code = _country_code(location, venue or "")
        link = card.find("a", href=True)
        if all((title, venue, location, country_code, link)):
            records.append(
                {
                    "title": title,
                    "date": start.date().isoformat(),
                    "url": link["href"],
                    "time_from": start.strftime("%H:%M"),
                    "venue": venue,
                    "city": location,
                    "country_code": country_code,
                    "description": "\n".join((title, venue_text)),
                }
            )
    return records


class ArrahmanCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="arrahman_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching performance page", event="crawler_url_fetch", url=PERFORMANCE_URL)
        page_response = requests.get(PERFORMANCE_URL, timeout=30)
        page_response.raise_for_status()

        records = []
        try:
            log_message("Fetching Framer CMS feed", event="crawler_url_fetch", url=CMS_URL)
            cms_response = requests.get(CMS_URL, timeout=30)
            cms_response.raise_for_status()
            records = _records_from_cms(cms_response.content)
        except requests.RequestException as error:
            log_message(
                "Framer CMS feed unavailable; using page fallback",
                event="crawler_fetch_fallback",
                url=CMS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )

        records.extend(_records_from_html(page_response.text))
        unique = {
            (record["title"], record["date"], record["venue"], record["city"]): record
            for record in records
        }
        result = list(unique.values())
        log_message("Performance scrape completed", event="crawler_scrape_completed", record_count=len(result))
        return result


def main():
    ArrahmanCrawler().run()


if __name__ == "__main__":
    main()
