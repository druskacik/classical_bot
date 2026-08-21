import html
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.jeanyvesthibaudet.com/"
CALENDAR_URL = urljoin(SOURCE_URL, "concerts")
SOURCE = "Jean-Yves Thibaudet"

COUNTRY_NAMES = {
    "argentina": "AR", "australia": "AU", "austria": "AT", "belgium": "BE",
    "brazil": "BR", "canada": "CA", "chile": "CL", "china": "CN",
    "czech republic": "CZ", "czechia": "CZ", "denmark": "DK", "finland": "FI",
    "france": "FR", "germany": "DE", "greece": "GR", "hong kong": "HK",
    "hungary": "HU", "iceland": "IS", "ireland": "IE", "italy": "IT",
    "japan": "JP", "lithuania": "LT", "luxembourg": "LU", "mexico": "MX", "monaco": "MC",
    "netherlands": "NL", "new zealand": "NZ", "norway": "NO", "poland": "PL",
    "portugal": "PT", "romania": "RO", "singapore": "SG", "south korea": "KR",
    "slovenia": "SI", "slovenija": "SI", "spain": "ES", "sweden": "SE",
    "switzerland": "CH", "taiwan": "TW",
    "united arab emirates": "AE", "united kingdom": "GB", "uk": "GB",
    "united states": "US", "usa": "US",
}

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
AUSTRALIAN_STATES = {"ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"}
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
}

TLD_COUNTRIES = {
    ".ar": "AR", ".at": "AT", ".au": "AU", ".be": "BE", ".br": "BR",
    ".ca": "CA", ".ch": "CH", ".cl": "CL", ".cn": "CN", ".cz": "CZ",
    ".de": "DE", ".dk": "DK", ".es": "ES", ".fi": "FI", ".fr": "FR",
    ".gr": "GR", ".hk": "HK", ".hu": "HU", ".ie": "IE", ".is": "IS",
    ".it": "IT", ".jp": "JP", ".kr": "KR", ".lu": "LU", ".mx": "MX",
    ".nl": "NL", ".no": "NO", ".nz": "NZ", ".pl": "PL", ".pt": "PT",
    ".ro": "RO", ".se": "SE", ".sg": "SG", ".tw": "TW", ".uk": "GB",
}


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def body_text(body):
    soup = BeautifulSoup(body or "", "html.parser")
    return clean_text(soup.get_text("\n")) or None


def country_from_links(soup):
    for link in soup.select("a[href]"):
        hostname = (urlparse(link.get("href", "")).hostname or "").lower()
        for suffix, code in TLD_COUNTRIES.items():
            if hostname.endswith(suffix):
                return code
    return None


def parse_location(body):
    soup = BeautifulSoup(body or "", "html.parser")
    columns = soup.select(".sqs-col-6")
    location_column = next(
        (column for column in columns if re.search(r"\bLocation\b", column.get_text(" "), re.I)),
        None,
    )
    if location_column is None:
        return None

    strong = location_column.find("strong")
    heading_lines = [clean_text(part) for part in strong.stripped_strings] if strong else []
    heading_lines = [part for part in heading_lines if part and part.casefold() != "location"]
    lines = [clean_text(part) for part in location_column.stripped_strings]
    lines = [part for part in lines if part and part.casefold() not in {"location", "more information"}]
    venue = heading_lines[0] if heading_lines else (lines[0] if lines else None)
    if not venue:
        return None
    if lines and lines[0] == venue:
        lines = lines[1:]
    while lines and lines[-1].casefold() in {
        "buy tickets", "canceled", "cancelled", "virtual concert", "tickets", "learn more",
    }:
        lines.pop()
    if len(lines) > 1 and re.fullmatch(r"[A-Z]{0,3}-?\d[A-Z\d -]{2,}", lines[-1], re.I):
        lines.pop()
    if not lines:
        return None

    country_code = None
    country_line = lines[-1].strip(" ,.").casefold()
    if country_line in COUNTRY_NAMES:
        country_code = COUNTRY_NAMES[country_line]
        lines.pop()

    if country_code is None and lines:
        embedded_country = re.search(r",\s*([A-Za-z ]+)\s*$", lines[-1])
        if embedded_country and embedded_country.group(1).casefold() in COUNTRY_NAMES:
            country_code = COUNTRY_NAMES[embedded_country.group(1).casefold()]
            lines[-1] = lines[-1][:embedded_country.start()].strip(" ,")

    if country_code is None and lines and lines[-1].strip(" ,.").upper() in US_STATES:
        country_code = "US"
        lines.pop()

    city = None
    if lines:
        city_line = lines[-1].strip(" ,")
        region_match = re.search(r"^(.+?),\s*([A-Z]{2,3})(?:\s+\d[\d -]*)?$", city_line)
        if region_match:
            city, region = clean_text(region_match.group(1)), region_match.group(2)
            if region in US_STATES:
                country_code = country_code or "US"
            elif region in CANADIAN_PROVINCES:
                country_code = country_code or "CA"
            elif region in AUSTRALIAN_STATES:
                country_code = country_code or "AU"
        else:
            if country_code == "TW" and "," in city_line:
                city_line = city_line.rsplit(",", 1)[-1].strip()
            elif country_code == "CN":
                city_match = re.search(r"\b\d{5,6}\s+([A-Za-zÀ-ž .'’-]+)$", city_line)
                if city_match:
                    city_line = city_match.group(1)
            city = re.sub(r"^(?:[A-Z]{1,3}-)?[\d ]{4,8},?\s+", "", city_line)
            city = re.sub(r"^\d{4,6},\s*", "", city)
            city = re.sub(r",\s*[A-Za-z ]+\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d$", "", city)
            city = re.sub(r"\s+\d[A-Z\d -]{2,}$", "", city).strip(" ,")
            city = re.sub(r",\s*[A-Z]{2}-\d.*$", "", city)
            city = re.sub(r"\s*\([^)]*\)\s+[A-Z]\d[A-Z]\s*[A-Z0-9]\d[A-Z0-9]$", "", city)
            city = re.sub(r"\s+[A-Z]{1,2}\d[A-Z\d]?(?:\s+[A-Z\d]+)?$", "", city)
            if country_code == "US" and "," in city:
                candidate, region_name = city.rsplit(",", 1)
                if region_name.strip().casefold() in US_STATE_NAMES:
                    city = candidate.strip()

    country_code = country_code or country_from_links(location_column)
    if (
        not city
        or city.casefold() == venue.casefold()
        or not country_code
        or re.search(r"\d", city)
        or re.search(r"\b(?:street|st\.?|avenue|ave\.?|road|rd\.?|boulevard|blvd\.?)\b", city, re.I)
    ):
        return None
    return venue, city, country_code


def parse_item(item):
    title = clean_text(item.get("title"))
    if re.match(r"^virtual concert\b", title, re.I):
        return None
    full_url = item.get("fullUrl")
    start_ms = item.get("startDate")
    location = parse_location(item.get("body"))
    if not title or not full_url or not start_ms or location is None:
        return None

    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_ms = item.get("endDate")
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc) if end_ms else None
    venue, city, country_code = location
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": urljoin(SOURCE_URL, full_url),
        "time_from": start.strftime("%H:%M"),
        "time_to": end.strftime("%H:%M") if end else None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": body_text(item.get("body")),
    }


class JeanYvesThibaudetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jeanyvesthibaudet_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalBot/1.0)"
        records = []
        seen_items = set()
        seen_offsets = set()
        offset = None

        while True:
            params = {"format": "json"}
            if offset is not None:
                params["offset"] = offset
            log_message("Fetching concert calendar page", event="crawler_url_fetch", url=CALENDAR_URL)
            response = session.get(CALENDAR_URL, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()
            for item in [*(payload.get("upcoming") or []), *(payload.get("past") or [])]:
                item_id = item.get("id")
                if not item_id or item_id in seen_items:
                    continue
                seen_items.add(item_id)
                record = parse_item(item)
                if record:
                    records.append(record)

            pagination = payload.get("pagination") or {}
            next_offset = pagination.get("nextPageOffset") if pagination.get("nextPage") else None
            if next_offset is None or next_offset in seen_offsets:
                break
            seen_offsets.add(next_offset)
            offset = next_offset

        log_message("Concert calendar parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    JeanYvesThibaudetCrawler().run()


if __name__ == "__main__":
    main()
