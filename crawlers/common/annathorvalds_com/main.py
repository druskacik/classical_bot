import html
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.annathorvalds.com/"
CALENDAR_URL = urljoin(SOURCE_URL, "performances")
SOURCE = "Anna Thorvaldsdottir"

COUNTRY_NAMES = {
    "australia": "AU", "austria": "AT", "belgium": "BE", "brazil": "BR",
    "canada": "CA", "china": "CN", "croatia": "HR", "czech republic": "CZ",
    "denmark": "DK", "estonia": "EE", "finland": "FI", "france": "FR",
    "germany": "DE", "greece": "GR", "hungary": "HU", "iceland": "IS",
    "ireland": "IE", "italy": "IT", "japan": "JP", "latvia": "LV",
    "lithuania": "LT", "mexico": "MX", "netherlands": "NL", "new zealand": "NZ",
    "norway": "NO", "poland": "PL", "portugal": "PT", "romania": "RO",
    "slovenia": "SI", "south korea": "KR", "spain": "ES", "sweden": "SE",
    "switzerland": "CH", "united kingdom": "GB", "uk": "GB",
    "united states": "US", "usa": "US",
}

# The calendar often omits the country after giving an unambiguous city.
CITY_COUNTRIES = {
    "a coruña": "ES", "adelaide": "AU", "amsterdam": "NL", "antwerpen": "BE",
    "bad vilbel": "DE", "barcelona": "ES", "basel": "CH", "berlin": "DE",
    "bodø": "NO", "boston": "US", "brussels": "BE", "chicago": "US",
    "cluj-napoca": "RO", "copenhagen": "DK", "den haag": "NL", "detroit": "US",
    "evanston": "US", "freiburg": "DE", "gothenburg": "SE", "haugesund": "NO",
    "helsinki": "FI", "ii": "FI", "leiden": "NL", "liverpool": "GB",
    "london": "GB", "los angeles": "US", "melbourne": "AU", "mexico city": "MX",
    "minneapolis": "US", "new york": "US", "nyc": "US", "odense": "DK",
    "oslo": "NO", "potsdam": "DE", "præstø": "DK", "reykjavik": "IS",
    "san francisco": "US", "santiago de compostela": "ES", "scottsdale": "US",
    "seattle": "US", "st. louis": "US", "stavanger": "NO", "stockholm": "SE",
    "sydney": "AU", "são paulo": "BR", "turku": "FI", "utrecht": "NL",
    "vancouver": "CA", "vienna": "AT", "wrocław": "PL", "youngstown": "US",
    "zeewolde": "NL", "zürich": "CH", "zwolle": "NL", "ársta": "SE",
}

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


def _clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _body_text(body):
    return _clean_text(BeautifulSoup(body or "", "html.parser").get_text(" "))


def _city_from_text(text, title):
    for haystack in (text, html.unescape(title)):
        candidates = []
        for city in sorted(CITY_COUNTRIES, key=len, reverse=True):
            matches = list(re.finditer(rf"(?<!\w){re.escape(city)}(?!\w)", haystack, re.I))
            if matches:
                candidates.append((matches[-1].start(), city))
        if candidates:
            city = max(candidates)[1]
            return "NYC" if city == "nyc" else city.title().replace("A Coruña", "A Coruña").replace("Cluj-Napoca", "Cluj-Napoca")

    patterns = (
        r"\bat\s+(?:the\s+)?[^.;]+?\s+in\s+([\wÀ-ž.'’ -]+?)(?:,\s*(?:[A-Z]{2}|[A-Za-z ]+))?[.;]",
        r"\bat\s+(?:the\s+)?[^.;,]+,\s*([\wÀ-ž.'’ -]+?)(?:,\s*[A-Z]{2})?[.;]",
        r"\bin\s+([\wÀ-ž.'’ -]+?),\s*(?:[A-Z]{2}|[A-Za-z ]+)[.;]",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            city = _clean_text(match.group(1)).strip(" ,")
            if 1 <= len(city.split()) <= 4:
                return city
    return None


def _country_from_text(text, city, body):
    if city and city.lower() in CITY_COUNTRIES:
        return CITY_COUNTRIES[city.lower()]
    lowered = text.lower()
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered):
            return code
    state_match = re.search(r",\s*([A-Z]{2})(?:[.;]|$)", text)
    if state_match and state_match.group(1) in US_STATES:
        return "US"

    soup = BeautifulSoup(body or "", "html.parser")
    domains = [urlparse(a.get("href", "")).hostname or "" for a in soup.select("a[href]")]
    tld_codes = {
        ".au": "AU", ".at": "AT", ".be": "BE", ".br": "BR", ".ca": "CA",
        ".ch": "CH", ".de": "DE", ".dk": "DK", ".es": "ES", ".fi": "FI",
        ".fr": "FR", ".hr": "HR", ".ie": "IE", ".is": "IS", ".it": "IT",
        ".jp": "JP", ".mx": "MX", ".nl": "NL", ".no": "NO", ".nz": "NZ",
        ".pl": "PL", ".pt": "PT", ".ro": "RO", ".se": "SE", ".si": "SI",
        ".uk": "GB",
    }
    for domain in domains:
        for suffix, code in tld_codes.items():
            if domain.endswith(suffix):
                return code
    return None


def _venue_from_text(text, city):
    if not city:
        return None
    escaped_city = re.escape(city)
    patterns = (
        r"\bat\s+(?:the\s+)?(.+?)(?=\s+at\s+|[.;]|$)",
        rf"\bat\s+(?:the\s+)?(.+?)\s+in\s+{escaped_city}(?:,|\.|;)",
        rf"\bat\s+(?:the\s+)?(.+?),\s*{escaped_city}(?:,|\.|;)",
        rf"\bat\s+(?:the\s+)?(.+?)\s+at\s+{escaped_city}(?:,|\.|;)",
        rf"\bat\s+(?:the\s+)?([^.;]*{escaped_city}[^.;]*?)(?:\.|;)",
    )
    matches = []
    for pattern in patterns:
        matches.extend(re.finditer(pattern, text, re.I))
    for match in sorted(matches, key=lambda candidate: candidate.start(), reverse=True):
            venue = _clean_text(match.group(1)).strip(" ,")
            venue = re.split(r"\b(?:performed|conducted|set to)\b", venue, flags=re.I)[-1].strip(" ,")
            venue = re.split(r"\s+-\s+as part of\b", venue, flags=re.I)[0].strip(" ,")
            venue = re.split(r"\s+-\s+by\b", venue, flags=re.I)[0].strip(" ,")
            venue = re.sub(rf"(?:\s+in|,|\s+at)\s+{escaped_city}(?:,.*)?$", "", venue, flags=re.I).strip(" ,")
            if (
                venue
                and venue.lower() != city.lower()
                and not re.match(r"^(?:by|with|as part of)\b", venue, re.I)
                and not re.search(r"\b(?:tour|hosted by)\b", venue, re.I)
                and not re.search(r"\b(?:focus on|go there|experience them)\b", venue, re.I)
                and len(venue) <= 160
            ):
                return venue
    return None


def _parse_item(item):
    title = _clean_text(item.get("title"))
    description = _body_text(item.get("body"))
    city = _city_from_text(description, title)
    country_code = _country_from_text(description, city, item.get("body"))
    venue = _venue_from_text(description, city)
    if not all((title, item.get("startDate"), item.get("fullUrl"), city, country_code, venue)):
        return None

    start = datetime.fromtimestamp(item["startDate"] / 1000, tz=timezone.utc)
    end_ms = item.get("endDate")
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc) if end_ms else None
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": urljoin(SOURCE_URL, item["fullUrl"]),
        "time_from": start.strftime("%H:%M"),
        "time_to": end.strftime("%H:%M") if end else None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description or None,
    }


class AnnaThorvaldsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="annathorvalds_com",
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
        offset = None
        seen_offsets = set()
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
            items = [*(payload.get("upcoming") or []), *(payload.get("past") or [])]
            for item in items:
                item_id = item.get("id")
                if item_id in seen_items:
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
    AnnaThorvaldsCrawler().run()


if __name__ == "__main__":
    main()
