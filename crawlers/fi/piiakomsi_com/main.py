import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Piia Komsi"
SOURCE_URL = "https://piiakomsi.com/calendar/"

# Some entries omit the city after a venue which identifies it unambiguously.
VENUE_CITIES = {
    "villa gyllenberg": "Helsinki",
    "hallé st peter's": "Manchester",
    "halle st peter's": "Manchester",
    "helsingin kaupunginteatteri": "Helsinki",
    "hietsun paviljonki": "Helsinki",
    "toivakan kirkko": "Toivakka",
}


def _clean_text(element) -> str:
    return re.sub(r"\n\s*\n+", "\n", element.get_text("\n", strip=True)).strip()


def _event_url(entry) -> str:
    links = [link.get("href", "").strip() for link in entry.select("a[href]")]
    for href in links:
        # Historical calendar entries contain this consistently malformed form.
        match = re.match(r"https?://https?:?//(piiakomsi\.com/events/.+)", href, re.I)
        if match:
            return "https://" + match.group(1)
    return SOURCE_URL


def _extract_time(text: str) -> str | None:
    patterns = (
        r"\b(?:klo|kl\.?|start time:)\s*(\d{1,2})[.:](\d{2})\b",
        r"\bat\s+(\d{1,2})[.:](\d{2})\s*(AM|PM)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if len(match.groups()) == 3:
            meridiem = match.group(3).upper()
            hour = hour % 12 + (12 if meridiem == "PM" else 0)
        if hour < 24 and minute < 60:
            return f"{hour:02d}:{minute:02d}"
    return None


def _extract_place(entry, description: str) -> tuple[str | None, str | None]:
    address_node = entry.select_one(".calendar-address")
    if not address_node:
        return None, None

    address = _clean_text(address_node)
    address = re.sub(r"\n?Event website\s*$", "", address, flags=re.I).strip(" ,\n")
    if not address:
        return None, None

    parts = [part.strip() for part in address.split(",") if part.strip()]
    venue = parts[0] if parts else None
    city = parts[-1] if len(parts) >= 2 and not re.search(r"\d", parts[-1]) else None

    mapped_city = VENUE_CITIES.get(venue.casefold()) if venue else None
    if mapped_city:
        city = mapped_city
    if city and city.casefold() == venue.casefold():
        city = None
    if not city:
        postal_city = re.search(r"\b\d{5}\s+([A-ZÅÄÖ][\wÅÄÖåäö' -]+)", description)
        if postal_city:
            city = postal_city.group(1).strip()
    return venue, city


def parse_calendar(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for entry in soup.select(".calendar-page-entry"):
        date_node = entry.select_one("h3.date")
        title_node = date_node.find_next_sibling("h3") if date_node else None
        if not date_node or not title_node:
            continue
        try:
            event_date = datetime.strptime(date_node.get_text(strip=True), "%d/%m/%Y").date().isoformat()
        except ValueError:
            continue

        description = _clean_text(entry)
        venue, city = _extract_place(entry, description)
        if not venue or not city:
            log_message(
                "Skipping calendar entry without a defensible venue or city",
                event="crawler_record_skipped",
                title=title_node.get_text(" ", strip=True),
                date=event_date,
            )
            continue

        records.append({
            "title": title_node.get_text(" ", strip=True),
            "date": event_date,
            "url": _event_url(entry),
            "time_from": _extract_time(description),
            "venue": venue,
            "city": city,
            "description": description,
        })
    return records


class PiiakomsiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="piiakomsi_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="FI",
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching calendar", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        records = parse_calendar(response.text)
        log_message(
            "Calendar parsed",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    PiiakomsiCrawler().run()


if __name__ == "__main__":
    main()
