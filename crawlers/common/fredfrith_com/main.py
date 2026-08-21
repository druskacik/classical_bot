import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.fredfrith.com/"
SOURCE = "Fred Frith"
FEED_URL = urljoin(SOURCE_URL, "live?format=json")
SITE_TIMEZONE = ZoneInfo("America/Los_Angeles")

# Squarespace's location objects on this site contain only a free-form
# addressTitle. These aliases cover the cities used throughout the published
# LIVE archive and allow touring performances to retain their real country.
CITY_COUNTRIES = {
    "Albany": "US", "Albuquerque": "US", "Baltimore": "US",
    "Berkeley": "US", "Denver": "US", "Flagstaff": "US",
    "Knoxville": "US", "Los Angeles": "US", "New York": "US",
    "Oakland": "US", "Philadelphia": "US", "Santa Cruz": "US",
    "Santa Fe": "US", "San Francisco": "US", "State College": "US",
    "Washington DC": "US", "Nanaimo": "CA", "Kelowna": "CA",
    "Courtenay": "CA", "Vancouver": "CA", "Victoria": "CA",
    "Montréal": "CA", "Québec City": "CA", "Amsterdam": "NL",
    "Antwerp": "BE", "Athens": "GR", "Baden": "CH", "Basel": "CH",
    "Belgrade": "RS", "Berlin": "DE", "Bonn": "DE", "Bratislava": "SK",
    "Bremen": "DE", "Brest": "FR", "Brussels": "BE", "Cerkno": "SI",
    "Copenhagen": "DK", "Forlí": "IT", "Geneva": "CH", "Hamburg": "DE",
    "Herdwangen-Schönach": "DE", "Huddersfield": "GB", "Köln": "DE",
    "Kortrijk": "BE", "Le Mans": "FR", "Leipzig": "DE", "Lille": "FR",
    "London": "GB", "Lübeck": "DE", "Luz St Sauveur": "FR",
    "Lyon": "FR", "Metz": "FR", "Milano": "IT", "Munich": "DE",
    "Nantes": "FR", "Oslo": "NO", "Palautordera": "ES", "Palermo": "IT",
    "Pantin": "FR", "Pardubice": "CZ", "Paris/Pantin": "FR",
    "Piacenza": "IT", "Poitiers": "FR", "Reims": "FR", "Roma": "IT",
    "Sankt Vith": "BE", "Salzburg": "AT", "Savona": "IT", "Siena": "IT",
    "St. Gallen": "CH", "Strasbourg": "FR", "Stuttgart": "DE",
    "Thessaloniki": "GR", "Vienna": "AT", "Villingen": "DE",
    "Wiesbaden": "DE", "Wuppertal": "DE", "Zagreb": "HR", "Zürich": "CH",
    "Łódź": "PL", "Luxembourg": "LU",
}

EXPLICIT_COUNTRIES = {
    "Belgium": "BE", "France": "FR", "Germany": "DE", "Poland": "PL",
    "Slovenia": "SI", "Switzerland": "CH", "UK": "GB",
}


def _clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip(" ,/")


def _parse_location(value):
    location = _clean_text(value)
    if not location or location.lower().startswith("recording session"):
        return None

    country_code = None
    for label, code in EXPLICIT_COUNTRIES.items():
        if re.search(rf"\b{re.escape(label)}\b", location, re.IGNORECASE):
            country_code = code
            break

    state_match = re.search(
        r"(?:,|\[)\s*(AZ|CA|CO|DC|NM|NY|PA|TN)(?:\]|\b)", location
    )
    if state_match:
        country_code = "US"
    elif re.search(r"\bBC\b", location):
        country_code = "CA"

    city = None
    # Prefer the longest alias so "Paris/Pantin" wins over "Pantin".
    for candidate in sorted(CITY_COUNTRIES, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", location, re.IGNORECASE):
            city = candidate
            country_code = country_code or CITY_COUNTRIES[candidate]
            break

    if not city or not country_code:
        return None

    # Keep establishment/festival names, but remove geographic and time
    # components from the free-form address before using it as the venue.
    venue_parts = []
    for part in re.split(r"\s*[,/]\s*", location):
        cleaned = _clean_text(part)
        if not cleaned or re.fullmatch(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?", cleaned, re.I):
            continue
        if re.match(r"^\d+\s", cleaned):
            continue
        if cleaned.casefold() == city.casefold():
            continue
        if cleaned in EXPLICIT_COUNTRIES or re.fullmatch(r"AZ|BC|CA|CO|DC|NM|NY|PA|TN", cleaned):
            continue
        cleaned = re.sub(rf"\b{re.escape(city)}\b", "", cleaned, flags=re.I).strip(" -[]")
        cleaned = re.sub(r"\[(?:New Mexico )?Jazz Festival\]", "", cleaned, flags=re.I).strip()
        if cleaned:
            venue_parts.append(cleaned)
    venue = _clean_text(", ".join(venue_parts))
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def _description_from_html(body):
    if not body:
        return None
    soup = BeautifulSoup(body, "html.parser")
    for element in soup(["style", "script"]):
        element.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    if text.casefold() in {"", "learn more", "buy tickets", "tickets"}:
        return None
    return text


def _get_page(session, url):
    log_message("Fetching event feed", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


class FredFrithCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="fredfrith_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "classical-events-crawler/1.0"})
        page_url = FEED_URL
        seen_pages = set()
        seen_items = set()
        records = []

        while page_url and page_url not in seen_pages:
            seen_pages.add(page_url)
            payload = _get_page(session, page_url)
            items = [*(payload.get("upcoming") or []), *(payload.get("past") or [])]

            for item in items:
                item_key = item.get("id") or item.get("fullUrl")
                if not item_key or item_key in seen_items:
                    continue
                seen_items.add(item_key)

                parsed_location = _parse_location((item.get("location") or {}).get("addressTitle"))
                start_ms = item.get("startDate")
                title = _clean_text(item.get("title"))
                full_url = item.get("fullUrl")
                if not parsed_location or not start_ms or not title or not full_url:
                    continue

                venue, city, country_code = parsed_location
                start = datetime.fromtimestamp(start_ms / 1000, SITE_TIMEZONE)
                end_ms = item.get("endDate")
                end = datetime.fromtimestamp(end_ms / 1000, SITE_TIMEZONE) if end_ms else None
                records.append({
                    "title": title,
                    "date": start.date().isoformat(),
                    "url": urljoin(SOURCE_URL, full_url),
                    "time_from": start.strftime("%H:%M:%S"),
                    "time_to": end.strftime("%H:%M:%S") if end else None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": _description_from_html(item.get("body")),
                })

            pagination = payload.get("pagination") or {}
            next_url = pagination.get("nextPageUrl") if pagination.get("nextPage") else None
            if next_url:
                separator = "&" if "?" in next_url else "?"
                page_url = urljoin(SOURCE_URL, f"{next_url}{separator}format=json")
            else:
                page_url = None

        log_message(
            "Fred Frith scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    FredFrithCrawler().run()


if __name__ == "__main__":
    main()
