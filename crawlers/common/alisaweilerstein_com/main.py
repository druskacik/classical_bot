import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Alisa Weilerstein"
SOURCE_URL = "https://alisaweilerstein.com/"
SCHEDULE_URL = urljoin(SOURCE_URL, "schedule/")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ClassicalBot/1.0; "
        "+https://github.com/ClassicalBot)"
    )
}

MONTH_PATTERN = (
    r"January|February|March|April|May|June|July|August|September|"
    r"October|November|December"
)
COUNTRY_CODES = {
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "canada": "CA",
    "china": "CN",
    "czech republic": "CZ",
    "czechia": "CZ",
    "denmark": "DK",
    "england": "GB",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "hong kong": "HK",
    "ireland": "IE",
    "italy": "IT",
    "japan": "JP",
    "luxembourg": "LU",
    "netherlands": "NL",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "scotland": "GB",
    "singapore": "SG",
    "south korea": "KR",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "taiwan": "TW",
    "united kingdom": "GB",
    "uk": "GB",
    "united states": "US",
    "usa": "US",
    "wales": "GB",
}
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \n\t,")


def clean_description(value: str) -> str:
    lines = [clean_text(line) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def expand_dates(value: str) -> list[str]:
    """Expand headings such as 'October 31 & November 2, 2026'."""
    match = re.fullmatch(rf"(.+),\s*(\d{{4}})", clean_text(value))
    if not match:
        return []

    date_part, year_text = match.groups()
    occurrences = re.findall(
        rf"(?:^|\s*(?:&|,)\s*)(?:({MONTH_PATTERN})\s+)?(\d{{1,2}})",
        date_part,
        flags=re.IGNORECASE,
    )
    current_month = None
    dates = []
    for month, day in occurrences:
        if month:
            current_month = month
        if current_month is None:
            return []
        try:
            parsed = datetime.strptime(
                f"{current_month} {day} {year_text}", "%B %d %Y"
            )
        except ValueError:
            continue
        dates.append(parsed.date().isoformat())
    return dates


def normalize_url(value: str) -> str:
    value = value.strip()
    if re.match(r"^[\w.-]+\.[a-z]{2,}/", value, flags=re.IGNORECASE):
        value = f"https://{value}"
    return urljoin(SCHEDULE_URL, value)


def get_country_code(country: str, city: str) -> str | None:
    country_text = clean_text(country)
    normalized = country_text.casefold()
    if normalized in COUNTRY_CODES:
        return COUNTRY_CODES[normalized]
    if country_text.upper() in US_STATE_CODES:
        return "US"

    city_region = re.search(r",\s*([A-Z]{2})$", city)
    if not normalized and city_region and city_region.group(1) in US_STATE_CODES:
        return "US"
    return None


class AlisaWeilersteinCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="alisaweilerstein_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "url", "venue", "description"],
        front_fields=[("source_url", SCHEDULE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching schedule", event="crawler_url_fetch", url=SCHEDULE_URL)
        response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        for event in soup.select(".event-date-wrap"):
            date_heading = event.select_one(".event-title")
            venue_node = event.select_one(".upcoming-venue")
            city_node = event.select_one(".upcoming-city")
            country_node = event.select_one(".upcoming-country")
            notes_node = event.select_one(".upcoming-notes")
            link_node = event.select_one("a.ticket-link[href]")

            dates = (
                expand_dates(date_heading.get_text(" ", strip=True))
                if date_heading
                else []
            )
            venue = (
                clean_text(venue_node.get_text(" ", strip=True)) if venue_node else ""
            )
            city = clean_text(city_node.get_text(" ", strip=True)) if city_node else ""
            country = clean_text(country_node.get_text(" ", strip=True)) if country_node else ""
            description = (
                clean_description(notes_node.get_text("\n", strip=True))
                if notes_node
                else None
            )
            url = (
                normalize_url(link_node.get("href", ""))
                if link_node
                else SCHEDULE_URL
            )
            country_code = get_country_code(country, city)

            if not dates or not venue or not city or not country_code:
                log_message(
                    "Skipping incomplete schedule item",
                    event="crawler_item_skipped",
                    url=url,
                    has_date=bool(dates),
                    has_venue=bool(venue),
                    has_city=bool(city),
                    has_country_code=bool(country_code),
                )
                continue

            title_detail = description.split("\n", 1)[0] if description else venue
            title = f"Alisa Weilerstein — {title_detail}"
            for date in dates:
                records.append(
                    {
                        "title": title,
                        "date": date,
                        "url": url,
                        "time_from": None,
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": description,
                    }
                )

        log_message(
            "Schedule parsed",
            event="crawler_scrape_completed",
            url=SCHEDULE_URL,
            record_count=len(records),
        )
        return records


def main():
    AlisaWeilersteinCrawler().run()


if __name__ == "__main__":
    main()
