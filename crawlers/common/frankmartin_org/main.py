import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Frank Martin Foundation"
SOURCE_URL = "https://www.frankmartin.org/"
API_URL = "https://www.frankmartin.org/wp-json/tribe/events/v1/events"
PAGE_SIZE = 50

COUNTRY_CODES = {
    "argentina": "AR", "australia": "AU", "austria": "AT",
    "belgium": "BE", "brazil": "BR", "canada": "CA", "chile": "CL",
    "china": "CN", "croatia": "HR", "czech republic": "CZ",
    "denmark": "DK", "estonia": "EE", "finland": "FI", "france": "FR",
    "germany": "DE", "greece": "GR", "hungary": "HU", "iceland": "IS",
    "india": "IN", "ireland": "IE", "israel": "IL", "italy": "IT",
    "japan": "JP", "latvia": "LV", "lithuania": "LT",
    "luxembourg": "LU", "mexico": "MX", "netherlands": "NL",
    "new zealand": "NZ", "norway": "NO", "poland": "PL",
    "portugal": "PT", "romania": "RO", "serbia": "RS",
    "singapore": "SG", "slovakia": "SK", "slovenia": "SI",
    "south africa": "ZA", "south korea": "KR", "spain": "ES",
    "sweden": "SE", "switzerland": "CH", "taiwan": "TW",
    "the netherlands": "NL", "turkey": "TR", "united kingdom": "GB",
    "united states": "US", "uruguay": "UY",
}

# The calendar also uses these abbreviations in older venue records.
COUNTRY_ABBREVIATIONS = {
    "A": "AT", "AT": "AT", "AUS": "AU", "B": "BE", "BE": "BE",
    "BR": "BR", "C": "CA", "CA": "CA", "CH": "CH", "CN": "CN",
    "CZ": "CZ", "D": "DE", "DE": "DE", "DK": "DK", "E": "ES",
    "EE": "EE", "ES": "ES", "F": "FR", "FI": "FI", "FR": "FR",
    "GB": "GB", "GR": "GR", "H": "HU", "HR": "HR", "I": "IT",
    "IE": "IE", "IL": "IL", "IRL": "IE", "IS": "IS", "ISR": "IL",
    "IT": "IT", "J": "JP", "JP": "JP", "L": "LU", "LT": "LT",
    "LV": "LV", "MEX": "MX", "N": "NO", "NL": "NL", "NO": "NO",
    "NZ": "NZ", "P": "PT", "PL": "PL", "PT": "PT", "RO": "RO",
    "S": "SE", "SE": "SE", "SG": "SG", "SK": "SK", "TR": "TR",
    "UK": "GB", "US": "US", "USA": "US", "ZA": "ZA",
}


def _clean_text(value):
    if not value:
        return None
    text = BeautifulSoup(html.unescape(str(value)), "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _country_code(venue):
    country = _clean_text(venue.get("country"))
    if country:
        code = COUNTRY_CODES.get(country.casefold())
        if code:
            return code
        code = COUNTRY_ABBREVIATIONS.get(country.upper().strip(". "))
        if code:
            return code

    venue_name = _clean_text(venue.get("venue")) or ""
    match = re.search(r"\(([A-Z]{1,3})\)(?:\s*\||\s*$)", venue_name)
    return COUNTRY_ABBREVIATIONS.get(match.group(1)) if match else None


def _venue_name(venue, city):
    name = _clean_text(venue.get("venue"))
    if not name:
        return None

    # Records commonly store "City (country) | Hall" as the venue name.
    if "|" in name:
        name = name.split("|", 1)[1].strip()
    name = re.sub(rf"^{re.escape(city)}\s*[-–,]\s*", "", name, flags=re.I).strip() if city else name
    if not name or (city and name.casefold() == city.casefold()):
        return None
    return name


def _event_record(event):
    title = _clean_text(event.get("title"))
    url = event.get("url")
    start_value = event.get("start_date")
    venue = event.get("venue") if isinstance(event.get("venue"), dict) else {}
    city = _clean_text(venue.get("city"))
    country_code = _country_code(venue)
    venue_name = _venue_name(venue, city)

    if not all((title, url, start_value, city, country_code, venue_name)):
        return None
    try:
        start = datetime.strptime(start_value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None

    time_from = None if event.get("all_day") else start.strftime("%H:%M:%S")
    time_to = None
    if not event.get("all_day") and event.get("end_date"):
        try:
            time_to = datetime.strptime(event["end_date"], "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S")
        except (TypeError, ValueError):
            pass

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": time_from,
        "time_to": time_to,
        "venue": venue_name,
        "city": city,
        "country_code": country_code,
        "description": _clean_text(event.get("description")),
    }


class FrankMartinCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="frankmartin_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        records = []
        page = 1
        params = {
            "per_page": PAGE_SIZE,
            "start_date": "2000-01-01 00:00:00",
            "end_date": "2100-12-31 23:59:59",
            "status": "publish",
        }
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalBot/1.0)"

        while True:
            log_message("Fetching events API page", event="crawler_url_fetch", url=API_URL, page=page)
            response = session.get(API_URL, params={**params, "page": page}, timeout=60)
            response.raise_for_status()
            payload = response.json()
            records.extend(
                record
                for event in payload.get("events", [])
                if (record := _event_record(event)) is not None
            )

            total_pages = int(payload.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1

        log_message("Events API scrape completed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    FrankMartinCrawler().run()


if __name__ == "__main__":
    main()
