import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://jamesprimosch.com/"
PERFORMANCES_URL = f"{SOURCE_URL}performances/"
SOURCE = "James Primosch"

US_REGIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
LOCATION_RE = re.compile(
    r"^(?P<city>[^,]+?)(?:,\s*|\s+)(?P<region>" + "|".join(sorted(US_REGIONS)) + r")"
    r"(?:\s*\([^)]*\))?$"
)
DATE_RE = re.compile(
    r"^(?P<month>[A-Za-z]+)\s+(?P<days>\d{1,2}(?:\s*,\s*\d{1,2})*)"
    r",\s*(?P<year>\d{4})\s*:\s*(?P<title>.*)$",
    re.DOTALL,
)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*([ap])\.?m\.?\b", re.IGNORECASE)
ADDRESS_RE = re.compile(
    r"^(?:\d+\s|.*\b(?:street|st\.|avenue|ave\.?|boulevard|blvd\.?|road|rd\.)\b)",
    re.IGNORECASE,
)
NON_VENUE_RE = re.compile(
    r"\b(?:soprano|mezzo|tenor|baritone|piano|pianist|violin|viola|cello|flute|"
    r"clarinet|oboe|saxophone|percussion|conductor|director|premiere|cancelled|"
    r"commissioned|streamed|recording session|additional performance|tba)\b",
    re.IGNORECASE,
)
VENUE_RE = re.compile(
    r"\b(?:hall|room|space|church|university|college|school|center|centre|museum|chapel|"
    r"conservatory|theatre|theater|auditorium|studio|synagogue|cathedral|club|"
    r"arts|academy|institute|institution|library|temple|bargemusic|the stone|"
    r"the cooperage|world café live|tanglewood)\b",
    re.IGNORECASE,
)


def _text_before_first_break(paragraph: Tag) -> str:
    parts = []
    for item in paragraph.descendants:
        if isinstance(item, Tag) and item.name == "br":
            break
        if isinstance(item, NavigableString):
            parts.append(str(item))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _lines(paragraph: Tag) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in paragraph.get_text("\n", strip=True).splitlines()
        if line.strip()
    ]


def _location(line: str) -> tuple[str, str] | None:
    line = line.strip().rstrip(".;")
    match = LOCATION_RE.match(line)
    if match:
        return match.group("city").strip(), "US"

    country_endings = {
        ", Canada": "CA",
        ", France": "FR",
        ", Russia": "RU",
        ", UK": "GB",
        ", South Australia": "AU",
    }
    for ending, country_code in country_endings.items():
        if line.endswith(ending):
            city = line[: -len(ending)].split(",")[-1].strip()
            if city:
                return city, country_code
    return None


def _venue(lines: list[str], location_index: int) -> str | None:
    for line in reversed(lines[1:location_index]):
        candidate = line.strip().rstrip(".;")
        if not candidate or ADDRESS_RE.search(candidate) or NON_VENUE_RE.search(candidate):
            continue
        if _location(candidate) or len(candidate) < 3 or not VENUE_RE.search(candidate):
            continue
        return candidate
    return None


def _dates(month: str, days: str, year: str) -> list[str]:
    parsed = []
    for day in re.findall(r"\d{1,2}", days):
        try:
            parsed.append(datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date().isoformat())
        except ValueError:
            return []
    return parsed


def _time(text: str) -> str | None:
    match = TIME_RE.search(text)
    if not match:
        return None
    hour, minute = map(int, match.group(1).split(":"))
    if match.group(2).lower() == "p" and hour != 12:
        hour += 12
    elif match.group(2).lower() == "a" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def parse_performances(html: bytes) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for paragraph in soup.select("main article p"):
        heading = _text_before_first_break(paragraph)
        match = DATE_RE.match(heading)
        if not match:
            continue

        title = re.sub(r"\s+", " ", match.group("title")).strip(" ;")
        if not title:
            continue
        lines = _lines(paragraph)
        locations = [(index, found) for index, line in enumerate(lines) if (found := _location(line))]
        # Entries containing several venues need event-specific pairing and cannot be
        # safely represented by a single venue/city record.
        if len(locations) != 1:
            continue
        location_index, (city, country_code) = locations[0]
        venue = _venue(lines, location_index)
        if not venue:
            continue

        description = "\n".join(lines)
        for event_date in _dates(match.group("month"), match.group("days"), match.group("year")):
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": PERFORMANCES_URL,
                    "time_from": _time(description),
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                }
            )
    return records


class JamesPrimoschCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jamesprimosch_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching performance archive", event="crawler_url_fetch", url=PERFORMANCES_URL)
        response = requests.get(
            PERFORMANCES_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=45,
        )
        response.raise_for_status()
        records = parse_performances(response.content)
        log_message(
            "Performance archive parsed",
            event="crawler_scrape_completed",
            url=PERFORMANCES_URL,
            record_count=len(records),
        )
        return records


def main():
    JamesPrimoschCrawler().run()


if __name__ == "__main__":
    main()
