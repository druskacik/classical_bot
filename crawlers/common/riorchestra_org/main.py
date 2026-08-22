import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.riorchestra.org/"
SOURCE = "Rachmaninoff International Orchestra"
COLLECTION_URL = f"{SOURCE_URL}concerts?format=json"

# Squarespace supplies country names rather than ISO codes in this collection.
# These are the countries represented in the published archive.
COUNTRY_CODES = {
    "China": "CN",
    "South Korea": "KR",
    "Spain": "ES",
    "Switzerland": "CH",
}

MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}

LOCATION_DATE_RE = re.compile(
    r"^(?P<first>\d{1,2})(?:-(?P<last>\d{1,2}))? "
    r"(?P<month>" + "|".join(MONTHS) + r")"
    r"(?:, (?P<time>\d{1,2}:\d{2}))?\s*~$"
)


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def page_text(html):
    return clean_text(BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True))


def body_lines(html):
    soup = BeautifulSoup(html or "", "html.parser")
    return [clean_text(line) for line in soup.get_text("\n", strip=True).splitlines() if clean_text(line)]


def iso_country(country):
    country = clean_text(country)
    code = COUNTRY_CODES.get(country)
    if code is None:
        log_message(
            "Skipping event with unsupported country",
            event="crawler_record_skipped",
            country=country,
        )
    return code


def make_record(item, *, title, date, time_from, venue, city, country_code, description):
    return {
        "title": title,
        "date": date,
        "url": f"{SOURCE_URL.rstrip('/')}{item['fullUrl']}",
        "time_from": time_from,
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description or None,
    }


def expanded_tour_records(item, description, year):
    """Expand concrete occurrences listed under a tour page's LOCATIONS heading."""
    lines = body_lines(item.get("body"))
    records = []
    for index, line in enumerate(lines):
        match = LOCATION_DATE_RE.match(line)
        if not match or index + 2 >= len(lines):
            continue

        city = clean_text(lines[index + 1])
        venue_line = clean_text(lines[index + 2])
        if not city or not venue_line.startswith("|"):
            continue
        venue = clean_text(venue_line.removeprefix("|"))
        if not venue:
            continue

        country_name = clean_text((item.get("location") or {}).get("addressCountry"))
        country_code = iso_country(country_name)
        if not country_code:
            continue

        first = int(match.group("first"))
        last = int(match.group("last") or first)
        month = MONTHS[match.group("month")]
        time_from = match.group("time")
        for day in range(first, last + 1):
            try:
                event_date = datetime(year, month, day).date().isoformat()
            except ValueError:
                continue
            records.append(
                make_record(
                    item,
                    title=item["title"],
                    date=event_date,
                    time_from=time_from,
                    venue=venue,
                    city=city,
                    country_code=country_code,
                    description=description,
                )
            )
    return records


def standard_record(item, timezone, description):
    location = item.get("location") or {}
    venue = clean_text(location.get("addressTitle"))
    address_line = clean_text(location.get("addressLine2"))
    city = clean_text(address_line.split(",", 1)[0])
    country_code = iso_country(location.get("addressCountry"))
    if not venue or not city or not country_code:
        return None

    start = datetime.fromtimestamp(item["startDate"] / 1000, tz=timezone)
    end = datetime.fromtimestamp(item["endDate"] / 1000, tz=timezone)
    record = make_record(
        item,
        title=clean_text(item.get("title")),
        date=start.date().isoformat(),
        time_from=start.strftime("%H:%M"),
        venue=venue,
        city=city,
        country_code=country_code,
        description=description,
    )
    if end.date() == start.date():
        record["time_to"] = end.strftime("%H:%M")
    return record


class RioOrchestraCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="riorchestra_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching concert collection", event="crawler_url_fetch", url=COLLECTION_URL)
        response = requests.get(COLLECTION_URL, timeout=30)
        response.raise_for_status()
        payload = response.json()
        timezone = ZoneInfo(payload.get("website", {}).get("timeZone", "America/New_York"))

        items = [*(payload.get("upcoming") or []), *(payload.get("past") or [])]
        records = []
        for item in items:
            title = clean_text(item.get("title"))
            full_url = clean_text(item.get("fullUrl"))
            if not title or not full_url or not item.get("startDate"):
                continue
            description = page_text(item.get("body")) or clean_text(item.get("excerpt"))
            start = datetime.fromtimestamp(item["startDate"] / 1000, tz=timezone)

            expanded = expanded_tour_records(item, description, start.year)
            if expanded:
                records.extend(expanded)
                continue

            record = standard_record(item, timezone, description)
            if record:
                records.append(record)

        log_message(
            "Concert collection parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    RioOrchestraCrawler().run()


if __name__ == "__main__":
    main()
