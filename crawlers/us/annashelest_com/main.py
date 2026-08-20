import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.annashelest.com/"
ARCHIVE_URL = "https://www.annashelest.com/calendar-details"
SOURCE = "Anna Shelest"

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
DATE_START_RE = re.compile(
    rf"(?im)^(?=(?:Monday|Tuesday|Tueday|Wednesday|Thursday|Thusrday|Friday|Saturday|Sunday)?"
    rf"\s*,?\s*(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b)"
)
DATE_RE = re.compile(
    rf"(?i)(?:{MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*[-–]\s*(\d{{1,2}})(?:st|nd|rd|th)?)?"
    r"(?:\s*,?\s*(\d{4}))?"
)
TIME_RE = re.compile(r"(?i)\b(1[0-2]|0?\d)(?::([0-5]\d))?\s*([ap])\.?m\.?")

US_STATE_RE = re.compile(
    r"\b([A-Z][A-Za-z.' -]{1,40}?),?\s+"
    r"(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)"
    r"(?:\s+\d{5})?\b"
)
INTERNATIONAL_LOCATIONS = {
    "Sèvremont": ("Sèvremont", "FR"),
    "Sevremont": ("Sèvremont", "FR"),
    "Tel Aviv": ("Tel Aviv", "IL"),
    "Tallinn": ("Tallinn", "EE"),
    "Mexico City": ("Mexico City", "MX"),
    "Cuernavaca": ("Cuernavaca", "MX"),
    "Budapest": ("Budapest", "HU"),
}
KNOWN_VENUES = {
    "WEILL RECITAL HALL AT CARNEGIE HALL": ("Weill Recital Hall at Carnegie Hall", "New York", "US"),
    "Weill Recital Hall at Carnegie Hall": ("Weill Recital Hall at Carnegie Hall", "New York", "US"),
    "FAUST HARRISON PIANOS": ("Faust Harrison Pianos", "New York", "US"),
    "Faust Harrison Pianos": ("Faust Harrison Pianos", "New York", "US"),
    "Bargemusic": ("Bargemusic", "Brooklyn", "US"),
    "Estonia Concert Hall": ("Estonia Concert Hall", "Tallinn", "EE"),
    "ESTONIA CONCERT HALL": ("Estonia Concert Hall", "Tallinn", "EE"),
}


def _clean(text):
    text = text.replace("\xa0", " ").replace("\ufeff", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _fetch(url):
    log_message("Fetching calendar page", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _calendar_texts(soup, archive=False):
    if not archive:
        node = soup.select_one("#calendar-page")
        return [node.get_text("\n", strip=True)] if node else []

    main = soup.select_one("main")
    if not main:
        return []
    return [node.get_text("\n", strip=True) for node in main.select(".sqs-html-content")]


def _split_events(text):
    text = _clean(text)
    starts = [match.start() for match in DATE_START_RE.finditer(text)]
    return [text[start:end].strip() for start, end in zip(starts, starts[1:] + [len(text)])]


def _times(header):
    values = []
    for match in TIME_RE.finditer(header):
        hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "p" else 0)
        values.append(f"{hour:02d}:{int(match.group(2) if match.group(2) else 0):02d}")
    return values


def _time_occurrences(header):
    times = _times(header)
    if len(times) >= 2:
        matches = list(TIME_RE.finditer(header))
        between = header[matches[0].end():matches[1].start()]
        if re.search(r"[-–—]\s*$", between):
            return [(times[0], times[1])]
    return [(value, None) for value in times] or [(None, None)]


def _dates(header, inherited_year):
    match = DATE_RE.search(header)
    if not match:
        return [], inherited_year
    year = int(match.group(3)) if match.group(3) else inherited_year
    if year is None:
        return [], inherited_year
    month = datetime.strptime(match.group(0).split()[0], "%B").month
    first, last = int(match.group(1)), int(match.group(2) or match.group(1))
    dates = []
    for day in range(first, last + 1):
        try:
            dates.append(datetime(year, month, day).date().isoformat())
        except ValueError:
            continue
    return dates, year


def _location(block):
    for marker, (city, country) in INTERNATIONAL_LOCATIONS.items():
        if re.search(rf"\b{re.escape(marker)}\b", block, re.IGNORECASE):
            return city, country

    matches = list(US_STATE_RE.finditer(block))
    if matches:
        city = matches[-1].group(1).strip(" (,\n")
        # Avoid swallowing a venue or street from a line with collapsed HTML.
        city = re.split(r"\n|\d{2,}|\b(?:Street|St\.|Avenue|Ave\.|Road|Rd\.)\b", city)[-1].strip()
        if city and not city.isupper():
            return city, "US"

    for name, (_, city, country) in KNOWN_VENUES.items():
        if name.lower() in block.lower():
            return city, country
    return None, None


def _venue(lines, block):
    venue_words = re.compile(
        r"(?i)\b(hall|auditorium|church|cathedral|center|centre|theatre|theater|museum|"
        r"university|college|academy|club|playhouse|chapel|symphony|orchestra|concert|arts)\b"
    )
    for line in lines[1:8]:
        if venue_words.search(line) and not TIME_RE.search(line):
            venue = re.sub(r"\s*\([^)]*,\s*[A-Z]{2}(?:\s+\d{5})?\).*?$", "", line)
            return venue.strip(" ()")
    for name, (venue, _, _) in KNOWN_VENUES.items():
        if name.lower() in block.lower():
            return venue
    return lines[1].strip(" ()") if len(lines) > 1 else None


def _records_from_text(text, url, inherited_year=None):
    records = []
    year = inherited_year
    pending_headers = []
    for block in _split_events(text):
        lines = [_clean(line) for line in block.splitlines() if _clean(line)]
        if not lines:
            continue
        header = lines[0]
        pending_headers.append(header)
        if len(lines) < 2:
            continue

        city, country = _location(block)
        venue = _venue(lines, block)
        if not city or not venue:
            pending_headers.clear()
            continue

        lowered = block.lower()
        if (
            "cd recording" in lowered
            or "web stream" in lowered
            or "shkolnikova academy" in lowered
            or "piano camp" in lowered
        ):
            pending_headers.clear()
            continue

        title = lines[1]
        for occurrence_header in pending_headers:
            dates, year = _dates(occurrence_header, year)
            for event_date in dates:
                for event_time, end_time in _time_occurrences(occurrence_header):
                    records.append({
                        "title": title,
                        "date": event_date,
                        "url": url,
                        "time_from": event_time,
                        "time_to": end_time,
                        "venue": venue,
                        "city": city,
                        "country_code": country,
                        "description": "\n".join(pending_headers + lines[1:]),
                    })
        pending_headers.clear()
    return records


class AnnaShelestCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="annashelest_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        records = []
        current = _fetch(SOURCE_URL)
        for text in _calendar_texts(current):
            records.extend(_records_from_text(text, SOURCE_URL))

        archive = _fetch(ARCHIVE_URL)
        for text in _calendar_texts(archive, archive=True):
            records.extend(_records_from_text(text, ARCHIVE_URL))

        log_message(
            "Parsed calendar records",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    AnnaShelestCrawler().run()


if __name__ == "__main__":
    main()
