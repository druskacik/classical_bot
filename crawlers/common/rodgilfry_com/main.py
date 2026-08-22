import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.rodgilfry.com/"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar-of-events")
SOURCE = "Rod Gilfry"

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        start=1,
    )
}

# The calendar often names the presenting company instead of the city. These
# are stable, unambiguous institutions/places, not guesses from the performer.
KNOWN_LOCATIONS = {
    "Metropolitan Opera": ("New York", "US"),
    "Houston Grand Opera": ("Houston", "US"),
    "Spoleto Festival dei Due Mondi": ("Spoleto", "IT"),
    "Nashville Symphony": ("Nashville", "US"),
    "Lyric Opera of Chicago": ("Chicago", "US"),
    "Sierra Madre, CA": ("Sierra Madre", "US"),
}


def _clean_text(element):
    return re.sub(r"\n{2,}", "\n", element.get_text("\n", strip=True)).strip()


def _parse_dates(value):
    """Expand the calendar's 'March 12, 15, April 3, 2027' notation."""
    year_match = re.search(r"\b(20\d{2})\b", value)
    if not year_match or re.search(r"\d\s*[-–—]\s*\d", value):
        return []

    year = int(year_match.group(1))
    month_matches = list(re.finditer("|".join(MONTHS), value, re.IGNORECASE))
    results = []
    for index, match in enumerate(month_matches):
        end = month_matches[index + 1].start() if index + 1 < len(month_matches) else year_match.start()
        day_text = value[match.end():end]
        for token in re.findall(r"\b(\d{1,2})(?:m\b)?", day_text, re.IGNORECASE):
            try:
                results.append(date(year, MONTHS[match.group().title()], int(token)).isoformat())
            except ValueError:
                continue
    return results


def _location(location_text):
    normalized = re.sub(r"\s+", " ", location_text).strip()
    for organization, result in KNOWN_LOCATIONS.items():
        if organization.casefold() in normalized.casefold():
            return result

    match = re.search(r"^([^,\n]+),\s*([A-Z]{2})\b", normalized)
    if match:
        city = match.group(1).strip()
        # State abbreviations establish the country, but an organization name
        # is not safe to transform into a city without stronger evidence.
        if not re.search(r"\b(opera|symphony|orchestra|festival|theatre|theater)\b", city, re.I):
            return city, "US"
    return None, None


def _event_groups(soup):
    groups = []
    current = []
    for block in soup.select("main .sqs-block"):
        classes = set(block.get("class", []))
        if "horizontalrule-block" in classes:
            if current:
                groups.append(current)
                current = []
        elif "html-block" in classes and _clean_text(block):
            current.append(block)
    if current:
        groups.append(current)
    return groups


def _parse_group(blocks):
    if len(blocks) != 2:
        return []

    detail = next((block for block in blocks if block.select_one("a[href]")), None)
    summary = next((block for block in blocks if block is not detail), None)
    if detail is None or summary is None:
        return []

    summary_lines = [line.strip() for line in summary.get_text("\n", strip=True).splitlines() if line.strip()]
    date_index = next(
        (index for index, line in enumerate(summary_lines) if re.search("|".join(MONTHS), line, re.I)),
        None,
    )
    if date_index is None:
        return []

    date_text = " ".join(summary_lines[date_index:])
    dates = _parse_dates(date_text)
    if not dates:
        return []

    title = summary_lines[0]
    location_text = " ".join(summary_lines[1:date_index])
    city, country_code = _location(location_text)
    venue_heading = detail.find(["h1", "h2", "h3", "h4"])
    venue = venue_heading.get_text(" ", strip=True) if venue_heading else None
    link = detail.select_one("a[href]")
    if not all((title, city, country_code, venue, link)):
        return []

    url = urljoin(CALENDAR_URL, link["href"])
    description = f"{_clean_text(summary)}\n{_clean_text(detail)}"
    return [
        {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": None,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }
        for event_date in dates
    ]


class RodGilfryCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="rodgilfry_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        groups = _event_groups(soup)
        for blocks in groups:
            records.extend(_parse_group(blocks))
        log_message(
            "Calendar parsed",
            event="crawler_scrape_completed",
            url=CALENDAR_URL,
            record_count=len(records),
            group_count=len(groups),
        )
        return records


def main():
    RodGilfryCrawler().run()


if __name__ == "__main__":
    main()
