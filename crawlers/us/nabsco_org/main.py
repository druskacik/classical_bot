import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.nabsco.org/"
SOURCE = "Narragansett Bay Symphony Community Orchestra"
EVENTS_URL = urljoin(SOURCE_URL, "events")
EVENTS_API_URL = f"{EVENTS_URL}?format=json"
TIME_ZONE = ZoneInfo("America/New_York")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_text(value):
    if not value:
        return ""
    soup = BeautifulSoup(str(value), "html.parser")
    for element in soup.select("style, script"):
        element.decompose()
    text = html.unescape(soup.get_text("\n", strip=True)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def event_datetime(timestamp):
    try:
        return datetime.fromtimestamp(int(timestamp) / 1000, tz=TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None


def city_from_location(location):
    address_line = clean_text((location or {}).get("addressLine2"))
    return address_line.split(",", 1)[0].strip() if address_line else ""


def parse_event(item):
    title = clean_text(item.get("title"))
    start = event_datetime(item.get("startDate"))
    end = event_datetime(item.get("endDate"))
    location = item.get("location") or {}
    venue = clean_text(location.get("addressTitle"))
    city = city_from_location(location)
    path = item.get("fullUrl") or (
        f"/events/{item['urlId']}" if item.get("urlId") else ""
    )
    url = urljoin(SOURCE_URL, path)

    if not title or not start or not path or not venue or not city:
        log_message(
            "Skipping event with incomplete required fields",
            event="crawler_event_skipped",
            level="warning",
            url=url or EVENTS_URL,
            has_title=bool(title),
            has_date=bool(start),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": start.strftime("%H:%M"),
        "time_to": end.strftime("%H:%M") if end else None,
        "venue": venue,
        "city": city,
        "country_code": "US",
        "description": clean_text(item.get("body")) or None,
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    records = []
    seen_pages = set()
    page_url = EVENTS_API_URL

    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        log_message("Fetching events page", event="crawler_url_fetch", url=page_url)
        response = session.get(page_url, headers=HEADERS, timeout=45)
        response.raise_for_status()
        payload = response.json()

        items = [
            *(payload.get("upcoming") or []),
            *(payload.get("past") or []),
            *(payload.get("items") or []),
        ]
        for item in items:
            record = parse_event(item)
            if record:
                records.append(record)

        next_path = (payload.get("pagination") or {}).get("nextPageUrl")
        if next_path:
            separator = "&" if "?" in next_path else "?"
            page_url = urljoin(SOURCE_URL, f"{next_path}{separator}format=json")
        else:
            page_url = None

    unique = {
        (record["url"], record["date"], record["time_from"]): record
        for record in records
    }
    result = sorted(
        unique.values(),
        key=lambda record: (record["date"], record["time_from"], record["title"]),
    )
    if not result:
        log_message(
            "No concerts found in events feed",
            event="crawler_empty_listing",
            level="warning",
            url=EVENTS_API_URL,
            record_count=0,
        )
    return result


class NabscoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nabsco_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "time_to",
            "venue",
            "city",
            "country_code",
            "description",
            "source_url",
            "source",
        ],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    NabscoOrgCrawler().run()


if __name__ == "__main__":
    main()
