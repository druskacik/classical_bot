import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.cameroncarpenter.com/"
SOURCE = "Cameron Carpenter"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar")
SITE_TIMEZONE = ZoneInfo("America/New_York")

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
COUNTRY_MARKERS = {
    "DE": "DE", "CN": "CN", "FR": "FR", "CH": "CH", "PT": "PT",
    "HU": "HU", "LU": "LU", "ES": "ES", "IT": "IT", "PL": "PL", "LV": "LV",
    "USA": "US", "OS": "AT",  # The calendar consistently uses OS for Austria.
}
CITY_COUNTRIES = {
    "MONACO": "MC", "ROTTERDAM": "NL", "AMSTERDAM": "NL", "BERLIN": "DE",
    "DRESDEN": "DE", "BONN": "DE", "LIEPAJA": "LV", "SHENZHEN": "CN",
    "LUXEMBOURG": "LU", "QUEBEC CITY": "CA", "DRESDEN": "DE",
    "ZURICH": "CH", "LUCERNE": "CH", "MUNICH": "DE", "BAMBERG": "DE",
    "TIMISOARA": "RO", "VIENNA": "AT", "SAN FRANCISCO": "US",
    "NEW YORK CITY": "US", "BEAVER CREEK": "US", "LAKEWOOD": "US",
    "TUCSON": "US", "BILBAO": "ES", "WASHINGTON DC": "US",
}


def _calendar_json(session, url):
    separator = "&" if "?" in url else "?"
    request_url = f"{url}{separator}format=json"
    log_message("Fetching calendar page", event="crawler_url_fetch", url=request_url)
    response = session.get(request_url, timeout=30)
    response.raise_for_status()
    return response.json()


def _clean_city_and_country(title, description):
    raw = unescape(title).strip()
    marker_match = re.search(r"\(([A-Z]{2,3})\)\s*$", raw)
    marker = marker_match.group(1) if marker_match else None
    if marker:
        raw = raw[:marker_match.start()].strip()
    else:
        comma_match = re.search(r",\s*([A-Z]{2})\s*$", raw)
        if not comma_match:
            comma_match = re.match(r"^(.+?),\s*([A-Z]{2})\b", raw)
        if comma_match:
            marker = comma_match.group(2) if comma_match.lastindex == 2 else comma_match.group(1)
            raw = (comma_match.group(1) if comma_match.lastindex == 2 else raw[:comma_match.start()]).strip()

    # A few US entries redundantly use "CITY, ST (USA)".
    state_match = re.search(r",\s*([A-Z]{2})\s*$", raw)
    if state_match:
        if not marker:
            marker = state_match.group(1)
        raw = raw[:state_match.start()].strip()

    normalized = re.sub(r"\s+", " ", raw).strip()
    normalized = re.sub(r"\s*\([12]/2\)$", "", normalized).strip()
    if normalized.upper() in CITY_COUNTRIES:
        country = CITY_COUNTRIES[normalized.upper()]
    elif marker_match and marker in COUNTRY_MARKERS:
        country = COUNTRY_MARKERS[marker]
    elif marker in US_STATES:
        country = "US"
    elif marker in CANADIAN_PROVINCES:
        country = "CA"
    else:
        country = COUNTRY_MARKERS.get(marker)

    if not country:
        text = description.casefold()
        names = {
            " germany": "DE", " switzerland": "CH", " austria": "AT",
            " romania": "RO", " spain": "ES", " luxembourg": "LU",
        }
        country = next((code for name, code in names.items() if name in text), None)
    return normalized.title(), country


def _venue_from_body(body):
    soup = BeautifulSoup(body or "", "html.parser")
    text = " ".join(soup.stripped_strings)

    # Recent entries use a consistent "presented ... at VENUE" sentence.
    at_matches = (re.findall(r"\bat\s+(.+?)(?:\s*\(film\))?[.]?$", text, re.IGNORECASE)
                  if re.match(r"^(Presented|With|At)\b", text, re.IGNORECASE) else [])
    match = re.search(r"^Presented.+?\bin\s+(.+?)[.]?$", text, re.IGNORECASE)
    candidate = at_matches[-1] if at_matches else (match.group(1) if match else None)
    if candidate:
        venue = candidate.strip(" .")
        venue = re.sub(r":\s+[^:]+,\s*(?:conductor|director)\b.*$", "", venue, flags=re.IGNORECASE)
        if 2 < len(venue) <= 180:
            return venue

    # Older entries put the venue in the first useful bold field.
    venue_words = re.compile(
        r"\b(hall|theat(?:re|er)|church|cathedral|basilica|philharmoni|concertgebouw|"
        r"auditori|center|centre|palace|palais|tonhalle|konzerthalle|kulturpalast|"
        r"sawdust|academy|university|nikolaikirche|duomo|chiesa|forum|stage)\b",
        re.IGNORECASE,
    )
    for tag in reversed(soup.select("strong")):
        venue = " ".join(tag.stripped_strings).strip(" .")
        rejected = {"link", "tickets", "calendar"}
        if (venue and venue.casefold() not in rejected and "tour" not in venue.casefold()
                and len(venue) <= 180 and venue_words.search(venue)):
            return venue

    # Some concise entries name a concert hall as the presenter without an "at" phrase.
    match = re.search(r"^Presented by\s+(.+?(?:Hall|Philharmonie|Philharmonic|Cathedral|Church|Center|Centre))[.]?$", text, re.IGNORECASE)
    if match:
        return match.group(1).strip(" .")

    # Early archive entries normally put an all-caps venue immediately before LINK.
    before_link = re.split(r"\bLINK\b", text, maxsplit=1, flags=re.IGNORECASE)[0].strip(" .")
    if before_link and len(before_link) <= 100 and "tour" not in before_link.casefold():
        # In several old entries the address shares a paragraph with an all-caps venue.
        uppercase_prefix = re.match(
            r"^([A-ZÀ-ÖØ-Þ0-9][A-ZÀ-ÖØ-Þ0-9 &'.,’\-]+?)(?=\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ])",
            before_link,
        )
        return uppercase_prefix.group(1).strip(" ,") if uppercase_prefix else before_link
    return None


def _event_record(item):
    description = "\n".join(BeautifulSoup(item.get("body") or "", "html.parser").stripped_strings)
    city, country_code = _clean_city_and_country(item.get("title") or "", description)
    venue = _venue_from_body(item.get("body"))
    if not city or not country_code or not venue or not item.get("startDate"):
        return None

    start = datetime.fromtimestamp(item["startDate"] / 1000, tz=SITE_TIMEZONE)
    end = datetime.fromtimestamp(item["endDate"] / 1000, tz=SITE_TIMEZONE) if item.get("endDate") else None
    detail_url = urljoin(SOURCE_URL, item.get("fullUrl") or f"calendar/{item['urlId']}")
    return {
        "title": f"Cameron Carpenter — {city}",
        "date": start.date().isoformat(),
        "url": detail_url,
        "time_from": start.strftime("%H:%M"),
        "time_to": end.strftime("%H:%M") if end else None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description or None,
    }


class CameronCarpenterCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="cameroncarpenter_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"})
        records = []
        seen_ids = set()
        page_url = CALENDAR_URL

        while page_url:
            payload = _calendar_json(session, page_url)
            for item in (payload.get("upcoming") or []) + (payload.get("past") or []):
                item_id = item.get("id")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                record = _event_record(item)
                if record:
                    records.append(record)
                else:
                    log_message(
                        "Skipping event with incomplete geography or venue",
                        event="crawler_event_skipped",
                        url=urljoin(SOURCE_URL, item.get("fullUrl") or "calendar"),
                    )

            pagination = payload.get("pagination") or {}
            next_path = pagination.get("nextPageUrl") if pagination.get("nextPage") else None
            page_url = urljoin(SOURCE_URL, next_path) if next_path else None

        return records


def main():
    CameronCarpenterCrawler().run()


if __name__ == "__main__":
    main()
