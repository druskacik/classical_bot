import html
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "The Colombo Wind Orchestra"
SOURCE_URL = "https://colombowindorchestra.lk/"
ARCHIVE_API = f"{SOURCE_URL}wp-json/wp/v2/past-concert"
REQUEST_TIMEOUT = 30

CITY_NAMES = (
    "Batticaloa",
    "Colombo",
    "Galle",
    "Kandy",
    "Kurunegala",
    "Polonnaruwa",
)


def _get(url, *, params=None):
    log_message("Fetching crawler URL", event="crawler_url_fetch", url=url)
    for attempt in range(4):
        response = requests.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "classical-bot crawler"},
        )
        if response.status_code < 500 or attempt == 3:
            response.raise_for_status()
            return response
        time.sleep(attempt + 1)


def _clean_html(value):
    soup = BeautifulSoup(value or "", "html.parser")
    for element in soup.select("style, script"):
        element.decompose()
    return re.sub(r"\s+", " ", html.unescape(soup.get_text(" "))).strip()


def _parse_date(text):
    match = re.search(
        r"\b(\d{1,2})\s*(?:st|nd|rd|th)?(?:\s+of)?\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r",?\s+(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2)} {match.group(3)}", "%d %B %Y"
        ).date().isoformat()
    except ValueError:
        return None


def _parse_time(text):
    match = re.search(r"\b(\d{1,2})[.:](\d{2})\s*([ap])\.?m\.?(?:\s+onwards)?\b", text, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "p":
        hour += 12
    return f"{hour:02d}:{match.group(2)}"


def _city_from_text(text):
    for city in CITY_NAMES:
        if re.search(rf"\b{re.escape(city)}\b", text, re.I):
            return city
    return None


def _archive_venue(text, city):
    date_match = re.search(
        r"\b\d{1,2}\s*(?:st|nd|rd|th)?(?:\s+of)?\s+"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)",
        text,
        re.I,
    )
    if date_match:
        prefix = text[: date_match.start()].strip(" .,")
        # The older entries consist of exactly "VENUE, CITY. DATE".
        if len(prefix) < 180 and city and not re.search(r"[a-z]", prefix):
            prefix = re.sub(rf",?\s*{re.escape(city)}(?:-\d+)?\.?$", "", prefix, flags=re.I)
            if prefix:
                return prefix.title()

    patterns = (
        r"\bin\s+(?:Colombo['’]s\s+)?(Bishop['’]s College Auditorium)\b",
        r"\bat (?:the iconic\s+)?(Lionel Wendt Theatre)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return None


def _archive_records():
    records = []
    page = 1
    while True:
        response = _get(
            ARCHIVE_API,
            params={"per_page": 100, "page": page, "orderby": "date", "order": "desc"},
        )
        items = response.json()
        for item in items:
            description = _clean_html(item.get("content", {}).get("rendered"))
            title = _clean_html(item.get("title", {}).get("rendered"))
            event_date = _parse_date(description)
            city = _city_from_text(description)
            venue = _archive_venue(description, city)
            if not all((title, event_date, item.get("link"), city, venue)):
                log_message(
                    "Skipping incomplete archive event",
                    event="crawler_record_skipped",
                    url=item.get("link"),
                    error_type="IncompleteEvent",
                    error_message="Required date, city, or venue is unavailable",
                )
                continue
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": item["link"],
                    "time_from": _parse_time(description),
                    "venue": venue,
                    "city": city,
                    "description": description or None,
                }
            )

        total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    return records


def _upcoming_records():
    soup = BeautifulSoup(_get(SOURCE_URL).text, "html.parser")
    section = soup.select_one("#upcoming")
    if not section:
        return []

    lines = [line.strip() for line in section.get_text("\n").splitlines() if line.strip()]
    records = []
    for index, line in enumerate(lines):
        event_date = _parse_date(line)
        if not event_date or index == 0 or index + 1 >= len(lines):
            continue
        title = lines[index - 1]
        venue_line = lines[index + 1]
        city = _city_from_text(venue_line)
        venue = re.split(r",", venue_line, maxsplit=1)[0].strip()
        if not all((title, city, venue)) or title.upper() in {"PRESENTS", "UPCOMING CONCERTS"}:
            continue
        records.append(
            {
                "title": title,
                "date": event_date,
                "url": f"{SOURCE_URL}#upcoming",
                "time_from": _parse_time(line),
                "venue": venue,
                "city": city,
                "description": re.sub(r"\s+", " ", section.get_text(" ")).strip() or None,
            }
        )
    return records


class ColomboWindOrchestraCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="colombowindorchestra_lk",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="LK",
        upload_target="classical",
        dedupe_subset=["title", "date", "url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = _upcoming_records() + _archive_records()
        unique = {}
        for record in records:
            unique[(record["title"], record["date"], record["url"])] = record
        return list(unique.values())


def main():
    ColomboWindOrchestraCrawler().run()


if __name__ == "__main__":
    main()
