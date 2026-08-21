import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.jamesgalway.com/"
SOURCE = "Sir James Galway"
TOUR_URL = urljoin(SOURCE_URL, "tour")
REQUEST_TIMEOUT = 30
SITE_TIMEZONE = ZoneInfo("America/New_York")
US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    "MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|"
    "VA|WA|WV|WI|WY|DC"
)
COUNTRIES = {
    "usa": "US",
    "united states": "US",
    "uk": "GB",
    "united kingdom": "GB",
    "england": "GB",
    "ireland": "IE",
    "switzerland": "CH",
    "france": "FR",
    "italy": "IT",
    "canada": "CA",
    "israel": "IL",
}


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def country_code(item):
    location = item.get("location") or {}
    haystack = " ".join(
        clean_text(value).lower()
        for value in (
            location.get("addressCountry"),
            location.get("addressLine2"),
            item.get("title"),
        )
    )
    for name, code in COUNTRIES.items():
        if re.search(rf"\b{re.escape(name)}\b", haystack):
            return code
    if re.search(rf"(?:,|\s)\s*(?:{US_STATES})(?:\s|,|\d|$)", haystack, re.I):
        return "US"
    return None


def city_name(item, code):
    location = item.get("location") or {}
    title = clean_text(item.get("title"))
    address1 = clean_text(location.get("addressLine1"))
    address2 = clean_text(location.get("addressLine2"))
    combined = " | ".join((title, address1, address2))

    if code == "US":
        if address2 and re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", address2):
            return address2
        pattern = rf"(?:^|[-,])\s*([A-Za-z][A-Za-z .'-]*?)\s*,?\s+(?:{US_STATES})(?:\s|,|\d|$)"
        for value in (address2, address1, title):
            matches = re.findall(pattern, value, re.I)
            if matches:
                city = clean_text(matches[-1]).strip("-, ")
                return re.sub(r"^(?:NW|NE|SW|SE)\s+", "", city, flags=re.I)
    if code == "CA":
        match = re.search(
            r"([A-Za-z][A-Za-z .'-]+),\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b",
            combined,
            re.I,
        )
        if match:
            return clean_text(match.group(1)).strip("-, ")

    value = address2
    if value:
        value = re.sub(r"^\d[\d -]*\s+", "", value)
        value = value.split(",", 1)[0]
        value = re.sub(r"\s+[A-Z]{1,2}\d[A-Z\d ]*$", "", value)
        value = re.sub(r"\s+\d+$", "", value)
        if clean_text(value):
            return clean_text(value)

    for name in COUNTRIES:
        match = re.search(
            rf"(?:-|,|at)\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]+?)(?:,\s*|\s+){re.escape(name)}\b",
            title,
            re.I,
        )
        if match:
            return clean_text(match.group(1)).strip("-, ")
    return None


def description_text(body):
    if not body:
        return None
    text = BeautifulSoup(body, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text or None


def iter_events(session):
    params = {"format": "json"}
    seen_offsets = set()
    while True:
        log_message("Fetching tour feed page", event="crawler_url_fetch", url=TOUR_URL)
        response = session.get(TOUR_URL, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        yield from payload.get("upcoming", [])
        yield from payload.get("past", [])

        pagination = payload.get("pagination") or {}
        offset = pagination.get("nextPageOffset")
        if not pagination.get("nextPage") or offset is None or offset in seen_offsets:
            break
        seen_offsets.add(offset)
        params = {"format": "json", "offset": offset}


class JamesGalwayCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jamesgalway_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        records = []
        seen_ids = set()
        with requests.Session() as session:
            session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalBot/1.0)"
            for item in iter_events(session):
                item_id = item.get("id")
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                location = item.get("location") or {}
                venue = clean_text(location.get("addressTitle"))
                code = country_code(item)
                city = city_name(item, code)
                start_ms = item.get("startDate")
                if not all((item.get("title"), item.get("fullUrl"), start_ms, venue, city, code)):
                    continue

                start = datetime.fromtimestamp(start_ms / 1000, tz=SITE_TIMEZONE)
                end_ms = item.get("endDate")
                end = datetime.fromtimestamp(end_ms / 1000, tz=SITE_TIMEZONE) if end_ms else None
                records.append(
                    {
                        "title": clean_text(item["title"]),
                        "date": start.date().isoformat(),
                        "url": urljoin(SOURCE_URL, item["fullUrl"]),
                        "time_from": start.strftime("%H:%M"),
                        "time_to": end.strftime("%H:%M") if end else None,
                        "venue": venue,
                        "city": city,
                        "country_code": code,
                        "description": description_text(item.get("body")),
                    }
                )
        log_message(
            "Tour scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    JamesGalwayCrawler().run()


if __name__ == "__main__":
    main()
