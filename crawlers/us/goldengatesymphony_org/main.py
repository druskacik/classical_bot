import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.goldengatesymphony.org/"
SOURCE = "Golden Gate Symphony Orchestra & Chorus"
EVENTS_PATH = "/current-season"


def _clean_text(value):
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip() or None


def _country_and_timezone(item):
    location = item.get("location") or {}
    country = html.unescape(location.get("addressCountry") or "")
    combined = " ".join(
        str(value or "")
        for value in (
            item.get("title"),
            location.get("addressLine1"),
            location.get("addressLine2"),
            country,
        )
    )
    if re.search(r"\b(?:Switzerland|Scuol)\b", combined, re.I):
        return "CH", ZoneInfo("Europe/Zurich")
    return "US", ZoneInfo("America/Los_Angeles")


def _venue_and_city(item, description):
    location = item.get("location") or {}
    title = html.unescape(item.get("title") or "").strip()
    address_title = html.unescape(location.get("addressTitle") or "").strip()
    line1 = html.unescape(location.get("addressLine1") or "").strip()
    line2 = html.unescape(location.get("addressLine2") or "").strip()
    location_text = " ".join((line1, line2))

    venue = address_title or None
    if not venue and line1 and not re.match(r"^\d", line1):
        venue = line1
    if not venue and re.search(r"\b(?:SFSU|McKenna Theatre)\b", title, re.I):
        venue = "McKenna Theatre at San Francisco State University"

    city = None
    if re.search(r"\bScuol\b", " ".join((title, location_text)), re.I):
        city = "Scuol"
        venue = venue or "Gurlaina SA"
    elif re.search(r"\bBenicia\b", " ".join((title, location_text)), re.I):
        city = "Benicia"
        if re.search(r"Clock\s*Tower", title, re.I):
            venue = venue or "Benicia Clock Tower"
    elif re.search(r"\bSan Francisco\b", location_text, re.I):
        city = "San Francisco"
    elif re.search(r"\b(?:SFSU|McKenna Theatre)\b", title, re.I):
        city = "San Francisco"
    elif "Southern Pacific Brewery" in title:
        city = "San Francisco"
        venue = "Southern Pacific Brewing"
    elif re.search(r"\bHomestead\b", title, re.I):
        city = "San Francisco"
        venue = "The Homestead"

    # Some new Squarespace entries have the address only in their body.
    if not city and description and re.search(r"\bSan Francisco, CA\b", description):
        city = "San Francisco"

    return venue, city


def _parse_item(item):
    title = html.unescape(item.get("title") or "").strip()
    path = item.get("fullUrl")
    start_ms = item.get("startDate")
    if not title or not path or not isinstance(start_ms, (int, float)):
        return None

    description = _clean_text(item.get("body"))
    country_code, timezone = _country_and_timezone(item)
    start = datetime.fromtimestamp(start_ms / 1000, timezone)
    end_ms = item.get("endDate")
    end = (
        datetime.fromtimestamp(end_ms / 1000, timezone)
        if isinstance(end_ms, (int, float))
        else None
    )
    venue, city = _venue_and_city(item, description)
    if not venue or not city:
        log_message(
            "Skipping event without a defensible venue or city",
            event="crawler_record_skipped",
            url=urljoin(SOURCE_URL, path),
            error_type="MissingLocation",
        )
        return None

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": urljoin(SOURCE_URL, path),
        "time_from": start.strftime("%H:%M:%S"),
        "time_to": end.strftime("%H:%M:%S") if end else None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class GoldenGateSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="goldengatesymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        records = []
        seen_pages = set()
        page_url = urljoin(SOURCE_URL, f"{EVENTS_PATH}?format=json")

        while page_url and page_url not in seen_pages:
            seen_pages.add(page_url)
            log_message("Fetching events page", event="crawler_url_fetch", url=page_url)
            response = requests.get(page_url, timeout=30)
            response.raise_for_status()
            payload = response.json()

            items = [
                *(payload.get("upcoming") or []),
                *(payload.get("past") or []),
                *(payload.get("items") or []),
            ]
            for item in items:
                record = _parse_item(item)
                if record:
                    records.append(record)

            next_path = (payload.get("pagination") or {}).get("nextPageUrl")
            if next_path:
                separator = "&" if "?" in next_path else "?"
                page_url = urljoin(SOURCE_URL, f"{next_path}{separator}format=json")
            else:
                page_url = None

        # Squarespace repeats the boundary event on adjacent offset pages.
        unique = {}
        for record in records:
            key = (record["url"], record["date"], record["time_from"])
            unique[key] = record
        return list(unique.values())


def main():
    GoldenGateSymphonyCrawler().run()


if __name__ == "__main__":
    main()
