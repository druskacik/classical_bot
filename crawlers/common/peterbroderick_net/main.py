import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Peter Broderick"
SOURCE_URL = "https://www.peterbroderick.net/"
API_URL = f"{SOURCE_URL}index.php"
REQUEST_TIMEOUT = 45
PER_PAGE = 50

COUNTRY_CODES = {
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "canada": "CA",
    "czech republic": "CZ",
    "czechia": "CZ",
    "denmark": "DK",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "iceland": "IS",
    "ireland": "IE",
    "italy": "IT",
    "japan": "JP",
    "netherlands": "NL",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "united kingdom": "GB",
    "united states": "US",
    "united states of america": "US",
}

# The source currently mislabels its Utrecht venue as being in the United
# States. These unambiguous touring cities take precedence over that field.
CITY_COUNTRIES = {
    "utrecht": "NL",
}


def _clean_text(value):
    if not value:
        return None
    text = html.unescape(str(value))
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text or None


def _country_code(city, country):
    city_code = CITY_COUNTRIES.get(city.casefold()) if city else None
    if city_code:
        return city_code
    if not country:
        return None
    country = _clean_text(country)
    if re.fullmatch(r"[A-Za-z]{2}", country or ""):
        return country.upper()
    return COUNTRY_CODES.get((country or "").casefold())


def _fetch_page(page):
    params = {
        "rest_route": "/tribe/events/v1/events",
        "start_date": "1900-01-01",
        "per_page": PER_PAGE,
        "page": page,
    }
    log_message("Fetching event API page", event="crawler_url_fetch", url=API_URL, page=page)
    response = requests.get(
        API_URL,
        params=params,
        headers={"User-Agent": "classical-bot/1.0"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _parse_event(event):
    title = _clean_text(event.get("title"))
    url = _clean_text(event.get("url"))
    venue_data = event.get("venue") or {}
    venue = _clean_text(venue_data.get("venue"))
    city = _clean_text(venue_data.get("city"))
    country_code = _country_code(city, venue_data.get("country"))
    start_value = event.get("start_date")

    if not all((title, url, venue, city, country_code, start_value)):
        return None
    try:
        start = datetime.strptime(start_value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None

    end = None
    end_value = event.get("end_date")
    if end_value:
        try:
            end = datetime.strptime(end_value, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass

    description_parts = filter(
        None,
        (_clean_text(event.get("description")), _clean_text(event.get("excerpt"))),
    )
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": None if event.get("all_day") else start.strftime("%H:%M"),
        "time_to": None if event.get("all_day") or not end else end.strftime("%H:%M"),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": "\n".join(description_parts) or None,
    }


class PeterBroderickCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="peterbroderick_net",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        page = 1
        while True:
            payload = _fetch_page(page)
            for event in payload.get("events", []):
                record = _parse_event(event)
                if record:
                    records.append(record)

            total_pages = int(payload.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1

        records.sort(key=lambda record: (record["date"], record["time_from"] or "", record["url"]))
        log_message(
            "Parsed event archive",
            event="crawler_archive_parsed",
            url=API_URL,
            page_count=page,
            record_count=len(records),
        )
        return records


def main():
    PeterBroderickCrawler().run()


if __name__ == "__main__":
    main()
