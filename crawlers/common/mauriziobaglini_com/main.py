import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Maurizio Baglini"
SOURCE_URL = "https://www.mauriziobaglini.com/"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar-2")
SITE_TIMEZONE = ZoneInfo("Europe/Rome")

# The artist is based in Italy and the calendar marks touring appearances in
# other countries in the city portion of the title.
COUNTRY_MARKERS = {
    "argentina": "AR",
    "austria": "AT",
    "belgio": "BE",
    "belgium": "BE",
    "brasile": "BR",
    "brazil": "BR",
    "cina": "CN",
    "china": "CN",
    "eritrea": "ER",
    "francia": "FR",
    "france": "FR",
    "germania": "DE",
    "germany": "DE",
    "giappone": "JP",
    "japan": "JP",
    "poland": "PL",
    "polonia": "PL",
    "portogallo": "PT",
    "portugal": "PT",
    "spagna": "ES",
    "spain": "ES",
    "svizzera": "CH",
    "switzerland": "CH",
    "uruguay": "UY",
    "usa": "US",
    "united states": "US",
}
INVALID_VENUES = {
    "location tbc",
    "luogo da definire",
    "masterclass",
    "summer academy",
}


def _text_from_html(value):
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _city_and_country(value):
    country_code = "IT"
    lowered = value.casefold()
    for marker, code in COUNTRY_MARKERS.items():
        if re.search(rf"\b{re.escape(marker)}\b", lowered):
            country_code = code
            break

    city = re.sub(r"\s*\([^)]*\)\s*", " ", value).strip(" ,-–")
    return city, country_code


def _location_from_title(title):
    if "|" not in title:
        return None
    city_part, venue = (part.strip() for part in title.split("|", 1))
    venue = re.sub(r"\s+", " ", venue).strip(" ,-–")
    if not city_part or not venue or venue.casefold() in INVALID_VENUES:
        return None
    if "tbc" in venue.casefold() or "da definire" in venue.casefold():
        return None

    city, country_code = _city_and_country(city_part)
    if not city:
        return None
    return city, venue, country_code


def _event_to_record(event):
    location = _location_from_title(event.get("title", ""))
    timestamp = event.get("startDate")
    path = event.get("fullUrl")
    if not location or not isinstance(timestamp, (int, float)) or not path:
        return None

    starts_at = datetime.fromtimestamp(timestamp / 1000, tz=SITE_TIMEZONE)
    city, venue, country_code = location
    return {
        "title": event["title"].strip(),
        "date": starts_at.date().isoformat(),
        "url": urljoin(SOURCE_URL, path),
        "time_from": starts_at.strftime("%H:%M"),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": _text_from_html(event.get("body")),
    }


class MaurizioBagliniCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="mauriziobaglini_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date"],
    )

    def scrape(self):
        records = []
        seen_ids = set()
        seen_pages = set()
        next_url = f"{CALENDAR_URL}?format=json"
        session = requests.Session()

        while next_url:
            if next_url in seen_pages:
                raise RuntimeError(f"Calendar pagination repeated URL: {next_url}")
            seen_pages.add(next_url)
            log_message("Fetching calendar page", event="crawler_url_fetch", url=next_url)
            try:
                response = session.get(next_url, timeout=30)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    "Calendar page fetch failed",
                    event="crawler_url_fetch_failed",
                    url=next_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for event in (payload.get("upcoming") or []) + (payload.get("past") or []):
                event_id = event.get("id")
                if not event_id or event_id in seen_ids:
                    continue
                seen_ids.add(event_id)
                record = _event_to_record(event)
                if record:
                    records.append(record)

            pagination = payload.get("pagination") or {}
            next_path = pagination.get("nextPageUrl") if pagination.get("nextPage") else None
            if next_path:
                separator = "&" if "?" in next_path else "?"
                next_url = urljoin(SOURCE_URL, f"{next_path}{separator}format=json")
            else:
                next_url = None

        log_message(
            "Calendar scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    MaurizioBagliniCrawler().run()


if __name__ == "__main__":
    main()
