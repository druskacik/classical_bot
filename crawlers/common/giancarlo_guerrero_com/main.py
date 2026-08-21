import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.giancarlo-guerrero.com/"
SOURCE = "Giancarlo Guerrero"
CALENDAR_URL = f"{SOURCE_URL}concert-calendar"
REQUEST_TIMEOUT = 40

# This is an international touring calendar.  Squarespace's location objects are
# empty/defaulted, so the city printed by the publisher is the reliable location
# evidence.  Keep this list explicit: an unknown location is skipped rather than
# assigned the artist's home country.
CITY_COUNTRIES = {
    "a coruña": "ES",
    "amsterdam": "NL",
    "asturias": "ES",
    "atlanta": "US",
    "auckland": "NZ",
    "baltimore": "US",
    "bamberg": "DE",
    "berlin": "DE",
    "bilbao": "ES",
    "boston": "US",
    "brevard": "US",
    "brisbane": "AU",
    "bruges": "BE",
    "brugge": "BE",
    "brussels": "BE",
    "cardiff": "GB",
    "charlotte": "US",
    "chautauqua": "US",
    "chicago": "US",
    "cincinnati": "US",
    "cleveland": "US",
    "copenhagen": "DK",
    "cuyahoga falls": "US",
    "dallas": "US",
    "detroit": "US",
    "eugene": "US",
    "frankfurt": "DE",
    "gainesville": "US",
    "grand rapids": "US",
    "hague": "NL",
    "hannover": "DE",
    "houston": "US",
    "indianapolis": "US",
    "katowice": "PL",
    "la coruna": "ES",
    "la coruña": "ES",
    "lisbon": "PT",
    "london": "GB",
    "madrid": "ES",
    "nashville": "US",
    "new haven": "US",
    "new york": "US",
    "palermo": "IT",
    "philadelphia": "US",
    "portland": "US",
    "porto": "PT",
    "salt lake city": "US",
    "san francisco": "US",
    "san josé": "CR",
    "sarasota": "US",
    "seattle": "US",
    "são paulo": "BR",
    "swansea": "GB",
    "tanglewood": "US",
    "vail": "US",
    "wellington": "NZ",
    "wroclaw": "PL",
    "wrocław": "PL",
}

NON_VENUE_WORDS = (
    "festival",
    "orchestra",
    "orquestra",
    "orchester",
    "orkestra",
    "philharmonia",
    "philharmonic",
    "symphony",
)

VENUE_WORDS = (
    "amphitheater",
    "auditorium",
    "auditório",
    "barbican",
    "center",
    "centre",
    "concertgebouw",
    "concert hall",
    "flagey",
    "hall",
    "opera",
    "pac",
    "pavilion",
    "philharmonie",
    "politeama",
    "school",
    "theater",
    "theatre",
    "universit",
)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def body_lines(body: str) -> list[str]:
    soup = BeautifulSoup(body or "", "html.parser")
    return [clean_text(value) for value in soup.stripped_strings if clean_text(value)]


def description_from_html(body: str) -> str | None:
    soup = BeautifulSoup(body or "", "html.parser")
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text or None


def find_city(title: str, lines: list[str]) -> tuple[str, str] | None:
    evidence = clean_text(" ".join([title, *lines[:8]])).casefold()
    # Longest first prevents "Portland"-style substrings from winning over a
    # more specific place should one be added later.
    for city in sorted(CITY_COUNTRIES, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(city)}(?!\w)", evidence):
            display = next(
                (
                    candidate
                    for candidate in [title, *lines[:8]]
                    if re.search(rf"(?<!\w){re.escape(city)}(?!\w)", candidate, re.IGNORECASE)
                ),
                city,
            )
            match = re.search(rf"(?<!\w){re.escape(city)}(?!\w)", display, re.IGNORECASE)
            return (match.group(0) if match else city), CITY_COUNTRIES[city]
    return None


def is_physical_venue(value: str) -> bool:
    lowered = value.casefold()
    if not value:
        return False
    if any(word in lowered for word in VENUE_WORDS):
        return True
    return not any(word in lowered for word in NON_VENUE_WORDS)


def has_venue_word(value: str) -> bool:
    lowered = value.casefold()
    return any(word in lowered for word in VENUE_WORDS)


def find_venue(title: str, city: str, lines: list[str]) -> str | None:
    normalized = clean_text(title).replace("–", "-").replace("—", "-")
    parts = [clean_text(part) for part in normalized.split("-") if clean_text(part)]

    # Normal entries use "City - Venue, Orchestra".
    if parts and city.casefold() in parts[0].casefold() and len(parts) > 1:
        candidate = clean_text(parts[1].split(",")[0])
        if is_physical_venue(candidate):
            return candidate

    # A few older entries put the venue itself in the title (for example,
    # "Carnegie Hall - Stern Auditorium") and put the city only in the body.
    if parts and city.casefold() not in normalized.casefold():
        candidate = clean_text(normalized)
        if is_physical_venue(candidate) and has_venue_word(candidate):
            return candidate

    # Body lines normally begin with date, city, then venue/ensemble.  Accept
    # only text that looks like a physical place, never an orchestra placeholder.
    for index, line in enumerate(lines[:8]):
        if city.casefold() not in line.casefold():
            continue
        for candidate in lines[index + 1 : index + 4]:
            candidate = clean_text(candidate.split(",")[0])
            if is_physical_venue(candidate) and has_venue_word(candidate):
                return candidate
    return None


def event_to_record(event: dict, timezone: ZoneInfo) -> dict | None:
    title = clean_text(event.get("title", ""))
    url_path = event.get("fullUrl")
    start_ms = event.get("startDate")
    if not title or not url_path or not isinstance(start_ms, (int, float)):
        return None

    lines = body_lines(event.get("body", ""))
    city_result = find_city(title, lines)
    if city_result is None:
        return None
    city, country_code = city_result
    venue = find_venue(title, city, lines)
    if venue is None:
        return None

    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone)
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": requests.compat.urljoin(SOURCE_URL, url_path),
        "time_from": start.time().replace(microsecond=0).isoformat(timespec="minutes"),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description_from_html(event.get("body", "")),
    }


class GiancarloGuerreroCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="giancarlo_guerrero_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers["User-Agent"] = "classical-bot concert crawler"
        offset = None
        seen_offsets: set[int] = set()
        events_by_id: dict[str, dict] = {}
        timezone = ZoneInfo("America/Chicago")

        while True:
            params = {"format": "json"}
            if offset is not None:
                params["offset"] = offset
            log_message("Fetching calendar page", event="crawler_url_fetch", url=CALENDAR_URL)
            response = session.get(CALENDAR_URL, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            payload = response.json()

            website_timezone = payload.get("website", {}).get("timeZone")
            if website_timezone:
                timezone = ZoneInfo(website_timezone)

            for item in payload.get("upcoming", []) + payload.get("past", []):
                event_id = item.get("id")
                if event_id:
                    events_by_id[event_id] = item

            pagination = payload.get("pagination") or {}
            if not pagination.get("nextPage"):
                break
            next_offset = pagination.get("nextPageOffset")
            if not isinstance(next_offset, int) or next_offset in seen_offsets:
                raise RuntimeError("Calendar pagination returned an invalid or repeated offset")
            seen_offsets.add(next_offset)
            offset = next_offset

        records = []
        for item in events_by_id.values():
            record = event_to_record(item, timezone)
            if record is not None:
                records.append(record)

        log_message(
            "Calendar parsed",
            event="crawler_parse_completed",
            record_count=len(records),
            skipped_count=len(events_by_id) - len(records),
        )
        return records


def main():
    GiancarloGuerreroCrawler().run()


if __name__ == "__main__":
    main()
