import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.jonathanleshnoff.com/"
CALENDAR_URL = urljoin(SOURCE_URL, "performances")
SOURCE = "Jonathan Leshnoff"
SITE_TIMEZONE = ZoneInfo("America/New_York")

COUNTRY_NAMES = {
    "australia": "AU", "austria": "AT", "belgium": "BE", "canada": "CA",
    "china": "CN", "czech republic": "CZ", "denmark": "DK", "finland": "FI",
    "france": "FR", "germany": "DE", "ireland": "IE", "israel": "IL",
    "italy": "IT", "japan": "JP", "mexico": "MX", "netherlands": "NL",
    "new zealand": "NZ", "norway": "NO", "poland": "PL", "portugal": "PT",
    "south korea": "KR", "spain": "ES", "sweden": "SE", "switzerland": "CH",
    "united kingdom": "GB", "uk": "GB", "united states": "US", "usa": "US",
    "vietnam": "VN", "việt nam": "VN",
}

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
US_STATE_NAMES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
}

# Some Squarespace entries retain map coordinates but omit the formatted address.
# These calendar venues recur and provide strong first-party evidence for the city.
VENUE_CITIES = {
    "augusta symphony": "Augusta",
    "baltimore symphony": "Baltimore",
    "bismarck mandan symphony orchestra": "Bismarck",
    "california theatre": "San Jose",
    "center for the arts at george mason university": "Fairfax",
    "center for the arts at george mason unversity": "Fairfax",
    "coronado theatre": "Rockford",
    "enoch pratt free library": "Baltimore",
    "hemmens cultural center": "Elgin",
    "joseph meyerhoff symphony hall": "Baltimore",
    "kleinhans music hall": "Buffalo",
    "mount baker theatre": "Bellingham",
    "mountain view high school": "Bend",
    "newport classical": "Newport",
    "orchestra hall": "Minneapolis",
    "schermerhorn symphony center": "Nashville",
    "second presbyterian church": "Baltimore",
    "st. ann catholic church": "Baltimore",
    "stefanie h. weill center for the performing arts": "Sheboygan",
    "tennessee theatre": "Knoxville",
    "the palace theatre, stamford": "Stamford",
    "việt nam national academy of music": "Hanoi",
    "weill hall, green music center": "Rohnert Park",
    "whitehead auditorium": "Valdosta",
}

CITY_COUNTRIES = {
    "Augusta": "US", "Baltimore": "US", "Bellingham": "US", "Bend": "US",
    "Bismarck": "US", "Buffalo": "US", "Camden": "US", "Cincinnati": "US",
    "Dallas": "US", "Elgin": "US", "Fairfax": "US", "Greenwich": "US",
    "Hanoi": "VN", "Knoxville": "US", "Minneapolis": "US", "Nashville": "US",
    "Newport": "US", "Oklahoma City": "US", "Rockford": "US",
    "Rohnert Park": "US", "San Jose": "US", "Sheboygan": "US",
    "Stamford": "US", "Valdosta": "US",
}


def _clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _description(body):
    text = BeautifulSoup(body or "", "html.parser").get_text(" ")
    return _clean_text(text) or None


def _city_from_location(location, description, title):
    venue = _clean_text(location.get("addressTitle"))
    mapped = VENUE_CITIES.get(venue.lower())
    if mapped:
        return mapped

    address = ", ".join(
        part for part in (
            _clean_text(location.get("addressLine1")),
            _clean_text(location.get("addressLine2")),
        ) if part
    )
    states = "|".join(re.escape(state) for state in sorted(US_STATE_NAMES, key=len, reverse=True))
    state_city = re.search(
        rf"(?:^|,)\s*([A-Za-z .'-]+?),\s*(?:[A-Z]{{2}}|{states})(?:[ ,]+\d{{5}})?(?:,|$)",
        address,
    )
    if state_city:
        candidate = _clean_text(state_city.group(1))
        if not re.search(r"\d", candidate):
            return candidate

    for city in sorted(CITY_COUNTRIES, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(city)}(?!\w)", f"{address} {description or ''} {title}", re.I):
            return city
    named_country = "|".join(re.escape(name) for name in sorted(COUNTRY_NAMES, key=len, reverse=True))
    city_country = re.search(
        rf"\b(?:in|at)\s+([A-Za-zÀ-ž .'-]+?),\s*(?:{named_country})\b",
        f"{title} {description or ''}",
        re.I,
    )
    if city_country:
        return _clean_text(city_country.group(1))
    if _clean_text(location.get("addressCountry")) and re.fullmatch(
        r"[A-Za-zÀ-ž .'-]+", _clean_text(location.get("addressLine2"))
    ):
        return _clean_text(location.get("addressLine2"))
    return None


def _country_code(location, city):
    country = _clean_text(location.get("addressCountry")).lower()
    if country in COUNTRY_NAMES:
        return COUNTRY_NAMES[country]
    if city in CITY_COUNTRIES:
        return CITY_COUNTRIES[city]

    address = " ".join(
        _clean_text(location.get(key))
        for key in ("addressLine1", "addressLine2", "addressCountry")
    )
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", address, re.I):
            return code
    state = re.search(r"(?:^|[, ])([A-Z]{2})(?:[ ,]+\d{5})?(?:,|$)", address)
    if state and state.group(1) in US_STATES:
        return "US"
    if any(re.search(rf"\b{re.escape(name)}\b", address, re.I) for name in US_STATE_NAMES):
        return "US"
    return None


def _venue(location):
    venue = _clean_text(location.get("addressTitle"))
    if venue:
        return venue
    line1 = _clean_text(location.get("addressLine1"))
    match = re.match(r"(.+?\D)\s+\d+\s+", line1)
    return _clean_text(match.group(1)) if match else None


def _parse_item(item):
    title = _clean_text(item.get("title"))
    location = item.get("location") or {}
    venue = _venue(location)
    description = _description(item.get("body"))
    city = _city_from_location(location, description, title)
    country_code = _country_code(location, city)
    start_ms = item.get("startDate")
    full_url = item.get("fullUrl")
    if not all((title, start_ms, full_url, venue, city, country_code)):
        return None

    start = datetime.fromtimestamp(start_ms / 1000, tz=SITE_TIMEZONE)
    end_ms = item.get("endDate")
    end = datetime.fromtimestamp(end_ms / 1000, tz=SITE_TIMEZONE) if end_ms else None
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": urljoin(SOURCE_URL, full_url),
        "time_from": start.strftime("%H:%M"),
        "time_to": end.strftime("%H:%M") if end else None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class JonathanLeshnoffCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jonathanleshnoff_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        records = []
        seen_items = set()
        seen_offsets = set()
        offset = None
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalBot/1.0)"

        while True:
            params = {"format": "json"}
            if offset is not None:
                params["offset"] = offset
            log_message("Fetching performance calendar page", event="crawler_url_fetch", url=CALENDAR_URL)
            response = session.get(CALENDAR_URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()

            for item in [*(payload.get("upcoming") or []), *(payload.get("past") or [])]:
                item_id = item.get("id")
                if not item_id or item_id in seen_items:
                    continue
                seen_items.add(item_id)
                record = _parse_item(item)
                if record:
                    records.append(record)

            pagination = payload.get("pagination") or {}
            next_offset = pagination.get("nextPageOffset") if pagination.get("nextPage") else None
            if next_offset is None or next_offset in seen_offsets:
                break
            seen_offsets.add(next_offset)
            offset = next_offset

        log_message("Performance calendar parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    JonathanLeshnoffCrawler().run()


if __name__ == "__main__":
    main()
