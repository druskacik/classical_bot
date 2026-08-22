import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Rachel Barton Pine"
SOURCE_URL = "https://www.rachelbartonpine.com/"
EVENTS_URL = urljoin(SOURCE_URL, "concerts")
HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/125.0 Safari/537.36"
    ),
}

COUNTRY_CODES = {
    "australia": "AU",
    "austria": "AT",
    "canada": "CA",
    "colombia": "CO",
    "costa rica": "CR",
    "france": "FR",
    "germany": "DE",
    "italy": "IT",
    "mexico": "MX",
    "netherlands": "NL",
    "spain": "ES",
    "switzerland": "CH",
    "united kingdom": "GB",
    "united states": "US",
    "usa": "US",
}
US_STATE_RE = re.compile(
    r"^(?:A[LKZR]|C[AOT]|DE|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|"
    r"N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|TN|TX|UT|V[AT]|W[AIVY])$",
    re.I,
)
DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})"
    r"(?:\s*(?:-|to)\s*(?:(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+)?\d{1,2})?\s*,\s*(\d{4})",
    re.I,
)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.I)


def clean_text(value, separator=" "):
    if not value:
        return None
    if "<" in str(value):
        value = BeautifulSoup(str(value), "html.parser").get_text(separator, strip=True)
    text = re.sub(r"\s+", " ", unescape(str(value))).strip()
    return text or None


def body_text(value):
    if not value:
        return None
    soup = BeautifulSoup(str(value), "html.parser")
    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line) or None


def parse_date_and_time(description, item):
    text = description or ""
    match = DATE_RE.search(text)
    if match:
        parsed_date = datetime.strptime(
            f"{match.group(1).title()} {match.group(2)} {match.group(3)}", "%B %d %Y"
        ).date().isoformat()
        line_end = text.find("\n", match.end())
        date_line = text[match.end():line_end if line_end >= 0 else len(text)]
        time_match = TIME_RE.search(date_line)
    else:
        try:
            parsed_date = datetime.utcfromtimestamp(int(item["startDate"]) / 1000).date().isoformat()
        except (KeyError, TypeError, ValueError, OverflowError):
            return None, None
        time_match = None

    time_from = None
    if time_match:
        hour = int(time_match.group(1)) % 12
        if time_match.group(3).lower() == "p":
            hour += 12
        time_from = f"{hour:02d}:{time_match.group(2) or '00'}"
    return parsed_date, time_from


def city_and_country(location, description):
    country_name = clean_text(location.get("addressCountry"))
    country_code = COUNTRY_CODES.get((country_name or "").casefold())

    location_match = re.search(r"(?im)^locations?:\s*([^\n]+)", description or "")
    location_parts = []
    if location_match:
        location_parts = [part.strip() for part in location_match.group(1).split(",") if part.strip()]

    address_line2 = clean_text(location.get("addressLine2"))
    address_parts = [part.strip() for part in (address_line2 or "").split(",") if part.strip()]
    city = address_parts[0] if address_parts else (location_parts[0] if location_parts else None)

    final_part = location_parts[-1] if len(location_parts) > 1 else None
    final_without_postcode = re.sub(r"\s+\d[\dA-Z -]*$", "", final_part or "").strip()
    if not country_code and US_STATE_RE.fullmatch(final_without_postcode):
        country_code = "US"
    if not country_code:
        country_code = COUNTRY_CODES.get(final_without_postcode.casefold())

    return clean_text(city), country_code


def parse_item(item):
    title = clean_text(item.get("title"))
    path = clean_text(item.get("fullUrl"))
    description = body_text(item.get("body") or item.get("excerpt"))
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    venue = clean_text(location.get("addressTitle"))
    city, country_code = city_and_country(location, description)
    date, time_from = parse_date_and_time(description, item)

    # Squarespace sometimes stores only a city and a dummy map pin.  The source
    # does not then identify a defensible venue, so those rows must be skipped.
    if not all((title, path, date, venue, city, country_code)):
        return None

    return {
        "title": title,
        "date": date,
        "url": urljoin(SOURCE_URL, path),
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


class RachelBartonPineComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="rachelbartonpine_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        columns=[
            "title", "date", "url", "time_from", "venue", "city",
            "country_code", "description", "source_url", "source",
        ],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self):
        records = []
        offset = None
        seen_offsets = set()

        while True:
            params = {"format": "json"}
            if offset is not None:
                params["offset"] = offset
            log_message("Fetching concert calendar", event="crawler_url_fetch", url=EVENTS_URL)
            response = requests.get(EVENTS_URL, params=params, headers=HEADERS, timeout=60)
            response.raise_for_status()
            payload = response.json()

            items = (payload.get("upcoming") or []) + (payload.get("past") or [])
            page_count = 0
            for item in items:
                record = parse_item(item)
                if record:
                    records.append(record)
                    page_count += 1
            log_message(
                "Concert calendar page parsed",
                event="crawler_page_parsed",
                url=response.url,
                record_count=page_count,
            )

            pagination = payload.get("pagination") or {}
            next_offset = pagination.get("nextPageOffset") if pagination.get("nextPage") else None
            if next_offset is None or next_offset in seen_offsets:
                break
            seen_offsets.add(next_offset)
            offset = next_offset

        return sorted(records, key=lambda row: (row["date"], row["time_from"] or "", row["title"]))


def main():
    RachelBartonPineComCrawler().run()


if __name__ == "__main__":
    main()
