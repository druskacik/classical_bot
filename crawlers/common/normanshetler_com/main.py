import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://normanshetler.com/"
SOURCE = "Norman Shetler"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/posts"

COUNTRY_CODES = {
    "Austria": "AT",
    "Germany": "DE",
    "Italy": "IT",
    "Japan": "JP",
    "Korea": "KR",
}

# The old calendar posts are free text, but use these venue names consistently.
# Only names actually stated on an individual post are accepted.
VENUES = (
    (r"Schubertkirche|Schubert Church", "Schubertkirche"),
    (r"Schloss Nieder Fellabrunn", "Schloss Nieder Fellabrunn"),
    (r"Krypta St\.?\s*Peters", "Krypta St Peters"),
    (r"Musikverein", "Musikverein Wien"),
    (r"St\.?\s*Michael Festival", "St. Michael"),
)


def _plain_text(rendered_html: str) -> str:
    soup = BeautifulSoup(rendered_html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _event_date(text: str) -> str | None:
    numeric = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if numeric:
        day, month, year = map(int, numeric.groups())
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    german = re.search(
        r"\b(\d{1,2})\.\s*(Januar|Februar|März|April|Mai|Juni|Juli|August|"
        r"September|Oktober|November|Dezember)\s+(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if german:
        month_numbers = {
            "januar": 1, "februar": 2, "märz": 3, "april": 4,
            "mai": 5, "juni": 6, "juli": 7, "august": 8,
            "september": 9, "oktober": 10, "november": 11, "dezember": 12,
        }
        day, month_name, year = german.groups()
        try:
            return datetime(int(year), month_numbers[month_name.lower()], int(day)).date().isoformat()
        except ValueError:
            return None

    named = re.search(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
        r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*(?:&|to|until|[-–])\s*\d{1,2})?,?\s+(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if not named:
        return None
    raw = named.group(0)
    first_day = re.sub(r"(?:\s*(?:&|to|until|[-–])\s*\d{1,2}).*$", "", raw)
    first_day = re.sub(r"(\d)(?:st|nd|rd|th)", r"\1", first_day, flags=re.IGNORECASE)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(first_day.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _time(text: str) -> str | None:
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(?:uhr|Uhr)\b", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _location(text: str) -> tuple[str, str] | None:
    # Modern archived performances name Wien and its postal district without a
    # country; the organization and venue text make Vienna, Austria explicit.
    if re.search(r"\b(?:Wien|Vienna)(?:\s+\d{4})?\b", text, re.IGNORECASE):
        return "Vienna", "AT"

    for country, code in COUNTRY_CODES.items():
        match = re.search(rf"\b([A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ .'-]+?),\s*{country}\b", text)
        if match:
            city = match.group(1).strip(" .,-")
            # Regions and multi-city itineraries are not valid city values.
            if city.lower() in {"bavaria"} or re.search(r"\band\b|,", city, re.IGNORECASE):
                return None
            return city, code
    return None


def _venue(text: str) -> str | None:
    for pattern, canonical in VENUES:
        if re.search(pattern, text, re.IGNORECASE):
            return canonical
    return None


def _record(post: dict) -> dict | None:
    description = _plain_text(post.get("content", {}).get("rendered", ""))
    title = html.unescape(_plain_text(post.get("title", {}).get("rendered", "")))
    evidence = f"{title} {description}"
    event_date = _event_date(evidence)
    location = _location(evidence)
    venue = _venue(evidence)
    if not event_date or not location or not venue:
        return None
    city, country_code = location
    return {
        "title": title,
        "date": event_date,
        "url": post["link"],
        "time_from": _time(evidence),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description or None,
    }


class NormanShetlerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="normanshetler_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        page = 1
        while True:
            params = {
                "per_page": 100,
                "page": page,
                "_fields": "link,title,content",
            }
            try:
                response = requests.get(API_URL, params=params, timeout=30)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch WordPress posts",
                    event="crawler_url_fetch_failed",
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            posts = response.json()
            for post in posts:
                record = _record(post)
                if record:
                    records.append(record)

            total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
            if page >= total_pages:
                break
            page += 1

        return records


def main():
    NormanShetlerCrawler().run()


if __name__ == "__main__":
    main()
