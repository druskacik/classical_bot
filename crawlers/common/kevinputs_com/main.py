import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.kevinputs.com/events"
SOURCE = "Kevin Puts"

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
COUNTRY_CODES = {
    "Germany": "DE",
    "UK": "GB",
    "United Kingdom": "GB",
    "Singapore": "SG",
}


def parse_dates(value: str) -> list[str]:
    """Expand the compact date notation used by the Webflow events collection."""
    value = " ".join(value.replace(",", " , ").split())
    year_match = re.search(r"\b(20\d{2})\b", value)
    if not year_match:
        return []
    year = int(year_match.group(1))
    text = value[:year_match.start()].strip(" ,")
    month_pattern = (
        r"January|February|March|April|May|June|July|August|September|"
        r"October|November|December"
    )
    month_matches = list(re.finditer(month_pattern, text))
    if not month_matches:
        return []

    results = []
    for index, match in enumerate(month_matches):
        month = match.group(0)
        end = month_matches[index + 1].start() if index + 1 < len(month_matches) else len(text)
        segment = text[match.end():end]
        days = [int(day) for day in re.findall(r"\b\d{1,2}\b", segment)]
        # A displayed run such as "November 7 to 15" does not disclose the
        # individual performance dates. Do not manufacture daily occurrences.
        if " to " in segment:
            return []
        for day in days:
            try:
                results.append(datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date().isoformat())
            except ValueError:
                continue
    return results


def parse_location(value: str) -> tuple[str, str, str] | None:
    parts = [part.strip() for part in re.split(r"[;,]", value) if part.strip()]
    if len(parts) < 2:
        return None

    tail = parts[-1]
    if tail in US_STATE_CODES:
        country_code = "US"
        city = parts[-2]
        venue_parts = parts[:-2]
    elif tail in COUNTRY_CODES:
        country_code = COUNTRY_CODES[tail]
        if len(parts) == 2 and tail == "Singapore":
            city = "Singapore"
            venue_parts = parts[:-1]
        else:
            city = parts[-2]
            venue_parts = parts[:-2]
    else:
        return None

    venue = ", ".join(venue_parts).strip()
    if not venue or not city:
        return None
    return venue, city, country_code


def parse_locations(value: str) -> list[tuple[str, str, str]]:
    """Parse one location, or multiple complete US locations separated by semicolons."""
    chunks = re.split(r"(?<=, [A-Z]{2});\s*", value)
    locations = [parse_location(chunk) for chunk in chunks]
    return [location for location in locations if location is not None]


class KevinPutsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="kevinputs_com",
        source=SOURCE,
        source_url="https://www.kevinputs.com/",
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching events page", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        for item in soup.select(".schedule-text4_item"):
            date_node = item.select_one("h5")
            title_node = item.select_one("h4")
            location_node = item.select_one(".small-text")
            if not date_node or not title_node or not location_node:
                continue

            dates = parse_dates(date_node.get_text(" ", strip=True))
            locations = parse_locations(location_node.get_text(" ", strip=True))
            title = title_node.get_text(" ", strip=True)
            if not dates or not locations or not title:
                continue
            link = item.select_one("a[href]")
            href = link.get("href", "") if link else ""
            url = urljoin(SOURCE_URL, href) if href and href != "#" else SOURCE_URL

            subtitle = item.select_one(".subtitle")
            rich_text = item.select_one(".w-richtext")
            description_parts = []
            for node in (subtitle, rich_text):
                if node:
                    text = node.get_text("\n", strip=True)
                    if text:
                        description_parts.append(text)
            description = "\n".join(description_parts) or None

            dated_locations = (
                zip(dates, locations) if len(dates) == len(locations)
                else ((event_date, locations[0]) for event_date in dates)
            )
            for event_date, location in dated_locations:
                venue, city, country_code = location
                records.append({
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": None,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                })

        log_message(
            "Parsed events page",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    KevinPutsCrawler().run()


if __name__ == "__main__":
    main()
