import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.anniegosfield.com/concerts10.html"
SOURCE = "Annie Gosfield"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0; +concert-indexer)"}
MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}
MONTHS["februray"] = 2  # Typo present in the first-party archive.

# The archive frequently names a venue but gives only a city abbreviation (or
# no city on that line). These are first-party venue/location pairs found on the
# page, not defaults applied to the artist's touring performances.
LOCATIONS = (
    ("Denver Museum of Nature and Science", "Denver", "US"),
    ("The Old Church", "Portland", "US"),
    ("Reed College, Eliot Chapel", "Portland", "US"),
    ("Beethoven-Haus", "Bonn", "DE"),
    ("La Jolla Music Society", "La Jolla", "US"),
    ("McKnight Center for the Performing Arts", "Stillwater", "US"),
    ("Herbst Theater", "San Francisco", "US"),
    ("Royal College of Music", "London", "GB"),
    ("Bologna Festival", "Bologna", "IT"),
    ("Chiesa di Santa Maria del Monte", "Cagliari", "IT"),
    ("Shandelee Music Festival", "Livingston Manor", "US"),
    ("YIVO", "New York", "US"),
    ("Walt Disney Concert Hall", "Los Angeles", "US"),
    ("Elbphilharmonie", "Hamburg", "DE"),
    ("Organo Hall", "Helsinki", "FI"),
    ("American Academy of Arts and Letters", "New York", "US"),
    ("Royal Holloway", "London", "GB"),
    ("Fine Arts Center", "Fayetteville", "US"),
    ("The Wolfsonian", "Miami Beach", "US"),
    ("Jacobs School of Music", "Bloomington", "US"),
    ("Carolina Theatre", "Greensboro", "US"),
    ("Arts On Site", "New York", "US"),
    ("Wiltshire Music Centre", "Bradford on Avon", "GB"),
    ("Shanghai botanical garden", "Shanghai", "CN"),
    ("University of Music and Performing Arts Frankfurt", "Frankfurt", "DE"),
    ("Oranjewoud Festival", "Oranjewoud", "NL"),
    ("Musical Storefronts", "New York", "US"),
    ("Bang on a Can Marathon, livestreamed from Berlin", "Berlin", "DE"),
    ("POLIN Museum", "Warsaw", "PL"),
    ("Phoenix Central Park", "Sydney", "AU"),
    ("MASS MoCA", "North Adams", "US"),
    ("Roulette", "Brooklyn", "US"),
    ("National Sawdust", "Brooklyn", "US"),
    ("The Stone", "New York", "US"),
    ("Spectrum", "New York", "US"),
    ("Merkin Hall", "New York", "US"),
    ("The Kitchen", "New York", "US"),
    ("Detroit Institute of Arts", "Detroit", "US"),
    ("Tivoli Vredenburg", "Utrecht", "NL"),
    ("Splendor", "Amsterdam", "NL"),
    ("Timucua Arts Foundation", "Orlando", "US"),
    ("First Unitarian Universalist Church of Austin", "Austin", "US"),
    ("Jessen Auditorium", "Austin", "US"),
    ("Visual Arts Center", "Austin", "US"),
)

DATE_RE = re.compile(
    r"^(?P<month>January|February|Februray|March|April|May|June|July|August|"
    r"September|October|November|December)\s*,?\s*(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(?P<year>20\d{2}|19\d{2}))?\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"\b(?P<hour>[01]?\d|2[0-3])(?::(?P<minute>\d{2}))?\s*(?P<ampm>[AP]M)?\b",
    re.IGNORECASE,
)


def _page_lines(container: Tag) -> list[tuple[str, str | None]]:
    """Return visual lines and the first detail link attached to each line."""
    lines: list[tuple[str, str | None]] = []
    text_parts: list[str] = []
    href: str | None = None

    def flush() -> None:
        nonlocal href
        text = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
        if text:
            lines.append((text, href))
        text_parts.clear()
        href = None

    def walk(node: Tag) -> None:
        nonlocal href
        for child in node.children:
            if isinstance(child, NavigableString):
                if str(child).strip():
                    text_parts.append(str(child))
            elif child.name == "br":
                flush()
            else:
                if child.name == "a" and child.get("href") and href is None:
                    href = urljoin(SOURCE_URL, child["href"])
                walk(child)

    walk(container)
    flush()
    return lines


def _blocks(lines: list[tuple[str, str | None]]) -> list[tuple[int, str, str | None]]:
    year: int | None = None
    blocks: list[tuple[int, str, str | None]] = []
    current: list[str] = []
    current_href: str | None = None

    def flush() -> None:
        nonlocal current_href
        if current and year is not None:
            blocks.append((year, " ".join(current), current_href))
        current.clear()
        current_href = None

    for text, href in lines:
        if re.fullmatch(r"(?:19|20)\d{2}", text):
            flush()
            year = int(text)
            continue
        if DATE_RE.match(text):
            flush()
            current.append(text)
            current_href = href
        elif current:
            if text not in {"INFO", "LINK", "VIDEO", "REVIEW", "LISTEN"}:
                current.append(text)
            current_href = current_href or href
    flush()
    return blocks


def _location(text: str) -> tuple[str, str, str] | None:
    folded = text.casefold()
    for venue, city, country_code in LOCATIONS:
        if venue.casefold() in folded:
            return venue, city, country_code
    return None


def _time(text: str, date_match: re.Match) -> str | None:
    # Search immediately after the date so years, addresses, and work titles do
    # not become times.
    tail = text[date_match.end():].lstrip(" ,")[:35]
    match = re.match(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*"
        + TIME_RE.pattern,
        tail,
        re.IGNORECASE,
    )
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").upper()
    if not ampm and re.search(r"PM\b", tail, re.IGNORECASE):
        ampm = "PM"
    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _title(text: str, date_match: re.Match) -> str:
    title = text[date_match.end():].lstrip(" ,:-")
    title = re.sub(r"^(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:AM|PM)?(?:\s+and\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM)?)?[, :] *", "", title, flags=re.I)
    title = title.split(". ", 1)[0].strip(" ,")
    title = re.sub(r"\s+(?:INFO|LINK|VIDEO|REVIEW|LISTEN)$", "", title)
    return title[:300]


class AnnieGosfieldCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="anniegosfield_com",
        source=SOURCE,
        source_url="https://www.anniegosfield.com/",
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "time_from", "title", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert archive", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        # The old hand-authored page changes containers partway through the
        # archive (h2 -> strong -> p), so parse its single top-level content div.
        container = soup.body.find("div", recursive=False) if soup.body else None
        if container is None or not container.get_text(strip=True):
            raise ValueError("Concert archive content was not found")

        records = []
        for section_year, text, href in _blocks(_page_lines(container)):
            match = DATE_RE.match(text)
            if match is None:
                continue
            year = int(match.group("year") or section_year)
            month = MONTHS[match.group("month").lower()]
            try:
                event_date = datetime(year, month, int(match.group("day"))).date().isoformat()
            except ValueError:
                continue
            location = _location(text)
            title = _title(text, match)
            excluded = re.search(
                r"\b(?:podcast|artist to artist talk|stone seminar|radio for open ears)\b",
                text,
                re.IGNORECASE,
            )
            if location is None or not title or excluded:
                continue
            venue, city, country_code = location
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": href or SOURCE_URL,
                    "time_from": _time(text, match),
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": text,
                }
            )

        log_message(
            "Concert archive parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
            url=SOURCE_URL,
        )
        return records


def main():
    AnnieGosfieldCrawler().run()


if __name__ == "__main__":
    main()
