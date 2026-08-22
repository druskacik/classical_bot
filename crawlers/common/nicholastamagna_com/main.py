import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://nicholastamagna.com/"
SOURCE = "Nicholas Tamagna"
AJAX_URL = "https://nicholastamagna.com/wp-admin/admin-ajax.php"
PAGE_SIZE = 20

COUNTRY_NAMES = {
    "austria": "AT",
    "belgium": "BE",
    "canada": "CA",
    "czech republic": "CZ",
    "czechia": "CZ",
    "denmark": "DK",
    "france": "FR",
    "germany": "DE",
    "italy": "IT",
    "netherlands": "NL",
    "poland": "PL",
    "spain": "ES",
    "switzerland": "CH",
    "united kingdom": "GB",
    "usa": "US",
    "united states": "US",
}

CITY_COUNTRIES = {
    "aarhus": "DK",
    "copenhagen": "DK",
    "københavn": "DK",
    "meiningen": "DE",
    "warsaw": "PL",
    "warschau": "PL",
}

TLD_COUNTRIES = {
    ".at": "AT",
    ".be": "BE",
    ".ca": "CA",
    ".ch": "CH",
    ".cz": "CZ",
    ".de": "DE",
    ".dk": "DK",
    ".es": "ES",
    ".fr": "FR",
    ".it": "IT",
    ".nl": "NL",
    ".pl": "PL",
    ".uk": "GB",
}


def _text(element) -> str | None:
    if element is None:
        return None
    value = element.get_text(" ", strip=True)
    return value or None


def _location_parts(location: str, event_url: str) -> tuple[str, str, str] | None:
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if not parts:
        return None

    country_code = None
    if parts[-1].casefold() in COUNTRY_NAMES:
        country_code = COUNTRY_NAMES[parts.pop().casefold()]

    location_folded = location.casefold()
    city = next((name.title() for name in CITY_COUNTRIES if name in location_folded), None)
    if city == "Warschau":
        city = "Warsaw"

    if country_code is None and city:
        country_code = CITY_COUNTRIES[city.casefold() if city.casefold() in CITY_COUNTRIES else "warsaw"]

    if country_code is None:
        hostname = (urlparse(event_url).hostname or "").casefold()
        country_code = next((code for tld, code in TLD_COUNTRIES.items() if hostname.endswith(tld)), None)

    if city is None and len(parts) >= 2:
        city = parts[-1]

    # The calendar sometimes supplies only a city and country, which is not a
    # defensible venue. Such records are deliberately skipped.
    venue_parts = parts[:-1] if city and parts and parts[-1].casefold() == city.casefold() else parts
    venue = ", ".join(venue_parts).strip()
    if not venue or venue.casefold() == (city or "").casefold():
        return None
    if not city or not country_code:
        return None
    return venue, city, country_code


def _parse_events(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for event in soup.select(".ttdm-gcal-event"):
        title = _text(event.select_one(".ttdm-gcal-event-title"))
        day = _text(event.select_one(".ttdm-gcal-event-day"))
        month = _text(event.select_one(".ttdm-gcal-event-month"))
        year = _text(event.select_one(".ttdm-gcal-event-year"))
        location = _text(event.select_one(".ttdm-gcal-event-location"))
        time_from = _text(event.select_one(".ttdm-gcal-event-time"))
        link = event.select_one(".ttdm-gcal-event-actions a[href]")
        event_url = link.get("href", "").strip() if link else SOURCE_URL
        if not all((title, day, month, year, location, event_url)):
            continue
        try:
            event_date = datetime.strptime(f"{day} {month} {year}", "%d %b %Y").date().isoformat()
        except ValueError:
            continue
        location_data = _location_parts(location.replace("📍", "").strip(), event_url)
        if location_data is None:
            continue
        venue, city, country_code = location_data
        records.append(
            {
                "title": title,
                "date": event_date,
                "url": event_url,
                "time_from": time_from,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": None,
            }
        )
    return records


class NicholasTamagnaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nicholastamagna_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        log_message("Fetching schedule", event="crawler_url_fetch", url=SOURCE_URL)
        response = session.get(SOURCE_URL, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        schedule = soup.select_one(".ttdm-gcal-events")
        if schedule is None:
            raise ValueError("Calendar event list was not found")
        records = _parse_events(str(schedule))
        nonce_script = next(
            (script.get_text() for script in soup.find_all("script") if "ttdmGcal" in script.get_text()),
            "",
        )
        nonce_match = re.search(r'"nonce"\s*:\s*"([^"]+)"', nonce_script)
        if not nonce_match:
            raise ValueError("Calendar pagination nonce was not found")

        load_more = soup.select_one(".ttdm-gcal-load-more-btn")
        offset = int(load_more.get("data-offset", len(records))) if load_more else len(records)
        while True:
            page = session.post(
                AJAX_URL,
                data={
                    "action": "ttdm_load_more_events",
                    "nonce": nonce_match.group(1),
                    "offset": offset,
                    "count": PAGE_SIZE,
                },
                timeout=30,
            )
            page.raise_for_status()
            payload = page.json()
            if not payload.get("success"):
                raise ValueError("Calendar pagination request was unsuccessful")
            data = payload.get("data") or {}
            loaded_count = int(data.get("loaded_count", 0))
            if loaded_count == 0:
                break
            records.extend(_parse_events(data.get("html", "")))
            offset = int(data.get("new_offset", offset + loaded_count))
            if loaded_count < PAGE_SIZE:
                break

        log_message(
            "Schedule scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    NicholasTamagnaCrawler().run()


if __name__ == "__main__":
    main()
