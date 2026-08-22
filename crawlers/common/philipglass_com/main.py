import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Philip Glass"
SOURCE_URL = "https://philipglass.com/"
EVENTS_API_URL = f"{SOURCE_URL}wp-json/tribe/events/v1/events"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
}

# The calendar is international. The Events Calendar API usually supplies the
# full country name; title-location fallbacks cover its older, incomplete venue
# records.
COUNTRY_CODES = {
    "argentina": "AR",
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "brazil": "BR",
    "bulgaria": "BG",
    "canada": "CA",
    "chile": "CL",
    "china": "CN",
    "colombia": "CO",
    "croatia": "HR",
    "czech republic": "CZ",
    "czech rep": "CZ",
    "czech rep.": "CZ",
    "czechia": "CZ",
    "denmark": "DK",
    "england": "GB",
    "estonia": "EE",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "greece": "GR",
    "holland": "NL",
    "hong kong": "HK",
    "hungary": "HU",
    "iceland": "IS",
    "india": "IN",
    "ireland": "IE",
    "israel": "IL",
    "italy": "IT",
    "japan": "JP",
    "korea": "KR",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "mexico": "MX",
    "netherlands": "NL",
    "new zealand": "NZ",
    "northern ireland": "GB",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "romania": "RO",
    "scotland": "GB",
    "serbia": "RS",
    "singapore": "SG",
    "slovakia": "SK",
    "slovenia": "SI",
    "south africa": "ZA",
    "south korea": "KR",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "taiwan": "TW",
    "the netherlands": "NL",
    "turkey": "TR",
    "united kingdom": "GB",
    "uk": "GB",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "wales": "GB",
}
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}
CANADIAN_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE",
    "QC", "SK", "YT",
}


def clean_text(value):
    if not value:
        return None
    text = html.unescape(str(value))
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def title_location(title):
    location = re.split(r"\s*\(", clean_text(title) or "", maxsplit=1)[0]
    parts = [part.strip() for part in location.split(",") if part.strip()]
    return parts


def country_code(event, venue):
    country = clean_text(venue.get("country"))
    if country:
        code = COUNTRY_CODES.get(country.casefold())
        if code:
            return code

    parts = title_location(event.get("title"))
    region = parts[-1] if len(parts) > 1 else ""
    normalized_region = region.rstrip(".").upper()
    if normalized_region in US_STATES:
        return "US"
    if normalized_region in CANADIAN_PROVINCES:
        return "CA"
    code = COUNTRY_CODES.get(region.casefold())
    if code:
        return code

    venue_name = clean_text(venue.get("venue")) or ""
    for label, candidate in COUNTRY_CODES.items():
        if re.search(rf"(?:^|,\s*){re.escape(label)}(?:\s*$|\b)", venue_name, re.I):
            return candidate
    return None


def event_city(event, venue):
    city = clean_text(venue.get("city"))
    if city:
        return city
    parts = title_location(event.get("title"))
    return parts[0] if parts else None


def event_venue(venue, city, country):
    name = clean_text(venue.get("venue"))
    if not name:
        return None

    # Some old venue records put an address into the venue-name field. Remove
    # explicit structured suffixes first, then obvious street-number suffixes.
    for suffix in (venue.get("address"), city, venue.get("state"), country):
        suffix = clean_text(suffix)
        if suffix:
            name = re.sub(rf",\s*{re.escape(suffix)}(?:\b|$).*$", "", name, flags=re.I)
    parts = [part.strip() for part in name.split(",") if part.strip()]
    kept = []
    address_hint = re.compile(
        r"(?:\d|\b(?:rd|road|st|street|ave|avenue|blvd|boulevard|lane|ln|"
        r"drive|dr|square|sq|pl|platz|plein|gade|gatan|ul)\.?\b)",
        re.I,
    )
    for index, part in enumerate(parts):
        if index and address_hint.search(part):
            break
        kept.append(part)
    name = ", ".join(kept)
    return name.strip(" ,") or None


def parse_event(event):
    title = clean_text(event.get("title"))
    url = clean_text(event.get("url"))
    raw_date = clean_text(event.get("start_date"))
    try:
        event_date = date.fromisoformat((raw_date or "")[:10]).isoformat()
    except ValueError:
        return None

    venue_data = event.get("venue") or {}
    country = clean_text(venue_data.get("country"))
    code = country_code(event, venue_data)
    city = event_city(event, venue_data)
    venue = event_venue(venue_data, city, country)
    if not all((title, url, city, venue, code)):
        return None

    time_from = None
    if not event.get("all_day"):
        details = event.get("start_date_details") or {}
        try:
            hour = int(details.get("hour"))
            minute = int(details.get("minutes"))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                time_from = f"{hour:02d}:{minute:02d}"
        except (TypeError, ValueError):
            pass

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": code,
        "description": clean_text(event.get("description")),
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


class PhilipglassComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="philipglass_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "venue",
            "city",
            "country_code",
            "description",
            "source_url",
            "source",
        ],
        dedupe_subset=["url", "date", "time_from", "venue", "city"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _get_page(self, url, params=None):
        log_message("Fetching Philip Glass events", event="crawler_url_fetch", url=url)
        response = self.session.get(url, params=params, timeout=45)
        response.raise_for_status()
        return response.json()

    def scrape(self):
        records = []
        skipped = 0
        url = EVENTS_API_URL
        params = {"start_date": "1900-01-01", "per_page": 50, "page": 1}
        pages_seen = 0

        while url:
            payload = self._get_page(url, params=params)
            params = None
            pages_seen += 1
            if pages_seen > 500:
                raise RuntimeError("Philip Glass event pagination exceeded 500 pages")

            for event in payload.get("events", []):
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    skipped += 1
            url = payload.get("next_rest_url")

        if skipped:
            log_message(
                "Skipped incomplete Philip Glass events",
                event="crawler_parse_warning",
                skipped_count=skipped,
                record_count=len(records),
            )
        return records


def main():
    PhilipglassComCrawler().run()


if __name__ == "__main__":
    main()
