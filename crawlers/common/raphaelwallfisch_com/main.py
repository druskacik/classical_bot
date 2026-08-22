import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Raphael Wallfisch"
SOURCE_URL = "https://raphaelwallfisch.com/"
# HTTPS is not served by the site at present, while this WordPress page is
# available over HTTP.  Keep the canonical public URL in crawler metadata.
CALENDAR_URL = "http://www.raphaelwallfisch.com/?page_id=41"

DATE_FORMATS = (
    "%d %B %Y",
    "%B %d %Y",
)

# The calendar is an international touring diary.  Locations are deliberately
# resolved from each listing rather than from the artist's home country.
CITY_COUNTRIES = {
    "Aldeburgh": "GB",
    "Aylsham": "GB",
    "Burford": "GB",
    "Cambridge": "GB",
    "Cheltenham": "GB",
    "Dorchester": "GB",
    "Gouda": "NL",
    "Haddington": "GB",
    "Haslemere": "GB",
    "Huddersfield": "GB",
    "Leamington Spa": "GB",
    "Leiden": "NL",
    "Liverpool": "GB",
    "Lohheide": "DE",
    "London": "GB",
    "Lüneburg": "DE",
    "Milngavie": "GB",
    "Morpeth": "GB",
    "Pinner": "GB",
    "Platt": "GB",
    "Poole": "GB",
    "Reading": "GB",
    "Ripon": "GB",
    "Snape": "GB",
    "Stockbridge": "GB",
    "Totnes": "GB",
    "Twickenham": "GB",
    "Wallingford": "GB",
    "Worcester": "GB",
}

COUNTRY_NAMES = {
    "France": "FR",
    "Germany": "DE",
    "Italy": "IT",
    "Netherlands": "NL",
    "Poland": "PL",
    "United Kingdom": "GB",
}

VENUE_WORDS = re.compile(
    r"\b(?:abbey|auditorium|chapel|church|college|hall|house|memorial|"
    r"pump room|room|school|theater|theatre|university)\b|\bst\.?\s+[a-z]",
    re.IGNORECASE,
)
UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE)
TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", re.IGNORECASE)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" ,")


def _parse_date_and_time(raw: str, default_year: int | None = None) -> tuple[str, str | None] | None:
    if re.search(r"\bto\b", raw, re.IGNORECASE):
        return None

    time_match = TIME_RE.search(raw)
    time_from = None
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        marker = (time_match.group(3) or "").lower()
        if marker == "pm" and hour != 12:
            hour += 12
        elif marker == "am" and hour == 12:
            hour = 0
        time_from = f"{hour:02d}:{minute:02d}"

    value = re.sub(r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Sun)\b", "", raw, flags=re.I)
    value = TIME_RE.sub("", value)
    value = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value, flags=re.I)
    value = _clean(value)
    if default_year is not None and not re.search(r"\b\d{4}\b", value):
        value = f"{value} {default_year}"
    for date_format in DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, date_format)
            return parsed.date().isoformat(), time_from
        except ValueError:
            continue
    return None


def _segment_after(date_node: Tag) -> tuple[list[str], str | None]:
    lines: list[str] = []
    current: list[str] = []
    event_url = None

    def flush():
        line = _clean(" ".join(current))
        if line:
            lines.append(line)
        current.clear()

    for sibling in date_node.next_siblings:
        if isinstance(sibling, Tag) and sibling.name == "strong":
            break
        if isinstance(sibling, Tag) and sibling.name == "br":
            flush()
            continue
        if isinstance(sibling, Tag) and sibling.name == "a":
            if event_url is None and sibling.get("href"):
                event_url = urljoin(CALENDAR_URL, sibling["href"])
            current.append(sibling.get_text(" ", strip=True))
        elif isinstance(sibling, NavigableString):
            current.append(str(sibling))
        elif isinstance(sibling, Tag):
            current.append(sibling.get_text(" ", strip=True))
    flush()
    return lines, event_url


def _location(text: str) -> tuple[str, str] | None:
    for city, country_code in sorted(CITY_COUNTRIES.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{re.escape(city)}\b", text, re.IGNORECASE):
            return city, country_code

    # A UK postcode is strong country evidence, but it is not enough to invent
    # a city.  The calendar's postcode-only listings include a locality nearby.
    if UK_POSTCODE.search(text):
        return None

    for country_name, country_code in COUNTRY_NAMES.items():
        if re.search(rf"\b{re.escape(country_name)}\b", text, re.IGNORECASE):
            # Only accept an explicit locality immediately before the country.
            match = re.search(rf"([A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.-]+)\s*,?\s*{re.escape(country_name)}\b", text)
            if match:
                return match.group(1), country_code
    return None


def _venue(lines: list[str], city: str) -> str | None:
    candidates = [line for line in lines if VENUE_WORDS.search(line)]
    if candidates:
        # Prefer the most venue-like line over a concert society/organizer link.
        candidate = min(candidates, key=len)
        # Some entries put the performer directly before the actual venue on
        # one line.  Do not leak that performer text into the venue field.
        embedded = re.search(r"\b([A-Z][\w’'-]+ School\b.*)", candidate)
        return embedded.group(1) if embedded else candidate
    if lines and city.lower() in lines[0].lower():
        candidate = lines[0]
        normalized = re.sub(
            r"\b(?:France|Germany|Italy|Netherlands|Poland|United Kingdom)\b",
            "",
            candidate,
            flags=re.I,
        ).strip(" ,").lower()
        if (
            normalized != city.lower()
            and not re.search(r"\b(?:festival|music society|concert society)\b", candidate, re.I)
        ):
            return candidate
    return None


def _title(lines: list[str], venue: str) -> str:
    artistic = []
    for line in lines:
        if venue in line and line != venue:
            prefix = _clean(line.split(venue, 1)[0])
            if prefix:
                artistic.append(prefix)
            continue
        if (
            line == venue
            or UK_POSTCODE.search(line)
            or re.search(r"\b(?:music society|concert society)\b", line, re.I)
            or (VENUE_WORDS.search(line) and len(line) > 45)
        ):
            continue
        artistic.append(line)
    if artistic:
        return _clean(" – ".join(artistic))
    return f"Raphael Wallfisch concert at {venue}"


def parse_calendar(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    previous_year = None
    for date_node in soup.select("p > strong"):
        parsed = _parse_date_and_time(date_node.get_text(" ", strip=True), previous_year)
        if parsed is None:
            continue
        date, time_from = parsed
        previous_year = int(date[:4])
        lines, event_url = _segment_after(date_node)
        description = "\n".join(lines)
        if not description or re.search(r"\brecording\b", description, re.IGNORECASE):
            continue
        location = _location(description)
        if location is None:
            continue
        city, country_code = location
        venue = _venue(lines, city)
        if venue is None:
            continue
        records.append({
            "title": _title(lines, venue),
            "date": date,
            "url": event_url or CALENDAR_URL,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        })
    return records


class RaphaelWallfischCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="raphaelwallfisch_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "url"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, timeout=30)
        response.raise_for_status()
        records = parse_calendar(response.text)
        log_message(
            "Concert calendar parsed",
            event="crawler_page_parsed",
            url=CALENDAR_URL,
            record_count=len(records),
        )
        return records


def main():
    RaphaelWallfischCrawler().run()


if __name__ == "__main__":
    main()
