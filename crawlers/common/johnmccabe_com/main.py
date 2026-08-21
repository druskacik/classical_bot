import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "John McCabe"
SOURCE_URL = "http://www.johnmccabe.com/"
ARCHIVE_URLS = (
    "http://www.johnmccabe.com/concert-archive.htm",
    "http://www.johnmccabe.com/older-concerts.htm",
)

COUNTRY_CODES = {
    "Australia": "AU",
    "Austria": "AT",
    "Belgium": "BE",
    "Canada": "CA",
    "France": "FR",
    "Germany": "DE",
    "Ireland": "IE",
    "Italy": "IT",
    "Japan": "JP",
    "Netherlands": "NL",
    "Norway": "NO",
    "Spain": "ES",
    "Sweden": "SE",
    "Switzerland": "CH",
    "United Kingdom": "GB",
}

DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) "
    r"\d{1,2} [A-Z][a-z]+ \d{4}"
)
TIME_RE = re.compile(
    r"^(?:about\s+)?(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)$", re.IGNORECASE
)
UK_POSTCODE_RE = re.compile(
    r"^(?:[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}|GIR\s*0AA)$", re.IGNORECASE
)
OTHER_POSTCODE_RE = re.compile(r"^(?:[A-Z]{0,3}[ .-]?)?\d[\d A-Z.-]{2,9}$", re.IGNORECASE)
UK_OUTWARD_RE = re.compile(r"^[A-Z]{1,2}\s*\d{1,2}[A-Z]?$", re.IGNORECASE)
UK_COUNTIES = {
    "Bedfordshire", "Berkshire", "Buckinghamshire", "Cambridgeshire",
    "Cheshire", "Cornwall", "Cumbria", "Derbyshire", "Devon", "Dorset",
    "Durham", "East Sussex", "Essex", "Gloucestershire", "Hampshire",
    "Hertfordshire", "Kent", "Lancashire", "Leicestershire", "Lincolnshire",
    "Merseyside", "Norfolk", "North Yorkshire", "Northamptonshire",
    "Northumberland", "Nottinghamshire", "Oxfordshire", "Shropshire",
    "Middlesex", "Somerset", "South Yorkshire", "Staffordshire", "Suffolk", "Surrey", "Sussex",
    "Tyne and Wear", "Warwickshire", "West Midlands", "West Sussex",
    "West Yorkshire", "Wiltshire", "Worcestershire",
}
REGIONS = {
    "British Columbia", "Nova Scotia", "New South Wales", "Victoria",
}
ADDRESS_WORD_RE = re.compile(
    r"\b(?:avenue|close|drive|lane|road|square|street)\b", re.IGNORECASE
)
NON_PERFORMANCE_PHRASES = (
    "question and answer session",
    "discusses his work as composer and pianist",
    "the composer in conversation",
    "day-school on sibelius",
    "workshop (details to be confirmed)",
    "choral workshop for composers",
)


def _lines(element):
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in element.get_text("\n", strip=True).splitlines()
        if line.strip()
    ]


def _parse_time(value):
    match = TIME_RE.fullmatch(value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3).lower()
    if hour > 12 or minute > 59:
        return None
    if suffix == "pm" and hour != 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}:00"


def _location_fields(lines):
    """Extract time, venue, city and country from an archive location cell."""
    try:
        country_index = next(i for i, line in enumerate(lines) if line in COUNTRY_CODES)
    except StopIteration:
        return None

    location = lines[:country_index]
    if not location:
        return None

    time_from = _parse_time(location[0])
    if time_from or location[0].lower() in {"time tba", "evening", "afternoon", "morning"}:
        location = location[1:]
    if len(location) < 2:
        return None

    venue = location[0].strip(" ,")
    address = [line.strip(" ,") for line in location[1:] if line.strip(" ,")]
    if not venue or not address:
        return None

    country = lines[country_index]
    candidates = []
    for line in address:
        if (
            UK_POSTCODE_RE.fullmatch(line)
            or UK_OUTWARD_RE.fullmatch(line)
            or OTHER_POSTCODE_RE.fullmatch(line)
            or re.fullmatch(r"(?:Vic(?:toria)?\.?|NSW)\s*\d{4}", line, re.IGNORECASE)
        ):
            continue
        line = re.sub(r"^\d{3}\s+\d{2}\s+", "", line).strip()
        line = re.sub(r"\s+[A-Z]{1,2}\s*\d{1,2}[A-Z]?$", "", line, flags=re.IGNORECASE).strip()
        if line:
            candidates.append(line)
    if not candidates:
        return None

    city = candidates[-1]
    if city in UK_COUNTIES or city in REGIONS:
        if len(candidates) < 2:
            return None
        city = candidates[-2]

    # Some addresses put the city and region/postcode on the same line.
    if "," in city:
        parts = [part.strip() for part in city.split(",") if part.strip()]
        if len(parts) > 1:
            city = parts[-2] if (parts[-1] in REGIONS or re.search(r"\d", parts[-1])) else parts[-1]

    if not city or city.casefold() == venue.casefold() or ADDRESS_WORD_RE.search(city):
        return None
    return time_from, venue, city, COUNTRY_CODES[country]


def _parse_page(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for heading in soup.find_all("h2"):
        date_text = re.sub(r"\s+", " ", heading.get_text(" ", strip=True))
        if not DATE_RE.fullmatch(date_text):
            continue

        heading_row = heading.find_parent("tr")
        event_row = heading_row.find_next_sibling("tr") if heading_row else None
        if event_row is None:
            continue
        cells = event_row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue

        left_lines = _lines(cells[0])
        location = _location_fields(left_lines)
        if location is None:
            continue
        time_from, venue, city, country_code = location

        right_lines = _lines(cells[1])
        if not right_lines:
            continue
        emphasized = cells[1].find(["i", "em"])
        title = (
            re.sub(r"\s+", " ", emphasized.get_text(" ", strip=True))
            if emphasized else right_lines[0]
        )
        if not title:
            continue

        description = "\n".join(right_lines)
        searchable_text = f"{title}\n{description}".casefold()
        if any(phrase in searchable_text for phrase in NON_PERFORMANCE_PHRASES):
            continue

        try:
            event_date = datetime.strptime(date_text, "%A %d %B %Y").date().isoformat()
        except ValueError:
            continue

        records.append({
            "title": title,
            "date": event_date,
            "url": page_url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        })
    return records


class JohnMcCabeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="johnmccabe_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        session = requests.Session()
        session.headers["User-Agent"] = "classical-concert-crawler/1.0"
        for url in ARCHIVE_URLS:
            log_message("Fetching concert archive", event="crawler_url_fetch", url=url)
            response = session.get(url, timeout=30)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "iso-8859-1"
            page_records = _parse_page(response.text, url)
            log_message(
                "Concert archive parsed",
                event="crawler_page_parsed",
                url=url,
                record_count=len(page_records),
            )
            records.extend(page_records)
        return records


def main():
    JohnMcCabeCrawler().run()


if __name__ == "__main__":
    main()
