import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.adamzukiewicz.com/"
SCHEDULE_URL = urljoin(SOURCE_URL, "schedule")
SOURCE = "Adam Piotr Żukiewicz"

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "January February March April May June July August September "
            "October November December"
        ).split(),
        start=1,
    )
}
MONTH_PATTERN = "|".join(MONTHS)
ENTRY_RE = re.compile(
    rf"^(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})"
    rf"(?:\s*-\s*(?:(?P<end_month>{MONTH_PATTERN})\s+)?(?P<end_day>\d{{1,2}}))?"
    r"\s*[:,]\s*(?P<body>.+)$",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"^(?P<time>\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))\s*:\s*",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"^20\d{2}$")

US_REGIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
    "Arizona", "California", "Colorado", "Illinois", "New York", "Ohio",
}
CANADIAN_REGIONS = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
COUNTRIES = {"Canada": "CA", "Italy": "IT", "Czechia": "CZ", "Poland": "PL"}
VENUE_WORDS = re.compile(
    r"\b(?:auditorium|campus commons|carnegie hall|church|college|conservatory|"
    r"gallery|highschool|high school|university)\b",
    re.IGNORECASE,
)
STEINWAY_RE = re.compile(r"\b(?:the\s+)?Steinway\s*&\s*Sons(?:\s+Piano\s+Gallery)?\b", re.IGNORECASE)
NON_EVENT_RE = re.compile(r"^(?:Adjudication|Faculty|Private Recital|Residency)\b", re.IGNORECASE)


def _normalise_time(value):
    if not value:
        return None
    cleaned = value.lower().replace(".", "").replace(" ", "")
    return datetime.strptime(cleaned, "%I:%M%p" if ":" in cleaned else "%I%p").strftime("%H:%M:%S")


def _location(body):
    """Return (venue, city, country), or None when the listing is too vague."""
    steinway_location = re.search(
        r"(?P<venue>(?:the\s+)?Steinway\s*&\s*Sons(?:\s+Piano\s+Gallery)?)\s+"
        r"(?P<city>[A-Za-z .'-]+),\s*(?P<region>[A-Z]{2})\b",
        body,
        re.IGNORECASE,
    )
    if steinway_location and steinway_location.group("region").upper() in US_REGIONS:
        return (
            re.sub(r"^the\s+", "", steinway_location.group("venue"), flags=re.IGNORECASE),
            steinway_location.group("city").strip(),
            "US",
        )

    canadian = re.search(r",\s*([^,]+),\s*([A-Z]{2}),\s*Canada\b", body)
    if canadian and canadian.group(2) in CANADIAN_REGIONS:
        city = canadian.group(1).strip()
        country_code = "CA"
        location_start = canadian.start(1)
    else:
        international = re.search(r",\s*([^,]+),\s*(Canada|Italy|Czechia|Poland)\b", body)
        if international:
            city = international.group(1).strip()
            country_code = COUNTRIES[international.group(2)]
            location_start = international.start(1)
        else:
            us = re.search(
                r",\s*([^,]+),\s*(" + "|".join(re.escape(region) for region in sorted(US_REGIONS, key=len, reverse=True)) + r")\b",
                body,
            )
            if not us:
                return None
            city_segment = us.group(1).strip()
            country_code = "US"
            location_start = us.start(1)
            steinway = STEINWAY_RE.search(city_segment)
            if steinway and city_segment[steinway.end():].strip():
                city = city_segment[steinway.end():].strip()
                venue = steinway.group(0).removeprefix("the ").removeprefix("The ")
                return venue, city, country_code
            city = city_segment

    if not city or "/" in city:
        return None

    before_city = body[:location_start].rstrip(" ,")
    segments = [segment.strip() for segment in before_city.split(",") if segment.strip()]
    for segment in reversed(segments):
        at_match = re.search(r"\bat\s+(?:the\s+)?(.+)$", segment, re.IGNORECASE)
        candidate = at_match.group(1).strip() if at_match else segment
        if re.match(r"^(?:Faculty|Recital and Masterclass)\b", candidate, re.IGNORECASE):
            continue
        if STEINWAY_RE.search(candidate) or VENUE_WORDS.search(candidate):
            return candidate, city, country_code
    return None


def _dates(year, match):
    start_month = MONTHS[match.group("month").title()]
    start_day = int(match.group("day"))
    end_day = int(match.group("end_day")) if match.group("end_day") else start_day
    end_month = MONTHS[match.group("end_month").title()] if match.group("end_month") else start_month
    if start_month != end_month:
        return [datetime(year, start_month, start_day).date()]
    return [datetime(year, start_month, day).date() for day in range(start_day, end_day + 1)]


def _schedule_entries(html):
    soup = BeautifulSoup(html, "html.parser")
    schedule = next(
        (node for node in soup.find_all(["h1", "h2", "p"]) if "January 24" in node.get_text(" ", strip=True)),
        None,
    )
    if schedule is None:
        raise ValueError("Could not locate the schedule text")

    year = None
    current = None
    entries = []
    for raw_line in schedule.get_text("\n", strip=True).splitlines():
        line = " ".join(raw_line.split())
        if YEAR_RE.fullmatch(line):
            year = int(line)
            continue
        match = ENTRY_RE.match(line)
        if match and year:
            current = {"year": year, "match": match, "links": []}
            entries.append(current)
        elif current and line.startswith(("http://", "https://", "www.")):
            current["links"].append(line if line.startswith("http") else f"https://{line}")
    return entries


class AdamZukiewiczCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="adamzukiewicz_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching schedule", event="crawler_url_fetch", url=SCHEDULE_URL)
        response = requests.get(SCHEDULE_URL, timeout=30)
        response.raise_for_status()

        records = []
        for entry in _schedule_entries(response.text):
            match = entry["match"]
            body = match.group("body").strip()
            time_match = TIME_RE.match(body)
            time_from = _normalise_time(time_match.group("time")) if time_match else None
            if time_match:
                body = body[time_match.end():].strip()
            if NON_EVENT_RE.match(body):
                continue
            location = _location(body)
            if not location:
                continue
            venue, city, country_code = location
            event_url = entry["links"][0] if entry["links"] else SCHEDULE_URL
            for event_date in _dates(entry["year"], match):
                records.append(
                    {
                        "title": body,
                        "date": event_date.isoformat(),
                        "url": event_url,
                        "time_from": time_from,
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": body,
                    }
                )

        log_message("Schedule parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    AdamZukiewiczCrawler().run()


if __name__ == "__main__":
    main()
