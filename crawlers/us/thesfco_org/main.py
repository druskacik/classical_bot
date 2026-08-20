from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.thesfco.org/"
SOURCE = "San Francisco Chamber Orchestra"
EVENTS_URL = urljoin(SOURCE_URL, "events?format=json")
LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")


def _description_from_html(html: str | None) -> str | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.select("style, script, noscript"):
        element.decompose()
    text = "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )
    return text or None


def _local_datetime(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, tz=LOCAL_TIMEZONE)


def _city_from_location(location: dict) -> str | None:
    address_line = location.get("addressLine2")
    if not address_line:
        return None
    city = address_line.split(",", 1)[0].strip()
    return city or None


def _record_from_item(item: dict) -> dict | None:
    location = item.get("location") or {}
    venue = (location.get("addressTitle") or "").strip()
    city = _city_from_location(location)
    title = (item.get("title") or "").strip()
    path = item.get("fullUrl")
    start_ms = item.get("startDate")

    if not (title and path and start_ms and venue and city):
        return None

    start = _local_datetime(start_ms)
    end_ms = item.get("endDate")
    end = _local_datetime(end_ms) if end_ms else None
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": urljoin(SOURCE_URL, path),
        "time_from": start.strftime("%H:%M:%S"),
        "time_to": end.strftime("%H:%M:%S") if end else None,
        "venue": venue,
        "city": city,
        "description": _description_from_html(item.get("body")),
    }


class TheSfcoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="thesfco_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        page_url = EVENTS_URL
        seen_pages = set()

        with requests.Session() as session:
            while page_url and page_url not in seen_pages:
                seen_pages.add(page_url)
                log_message("Fetching events page", event="crawler_url_fetch", url=page_url)
                response = session.get(page_url, timeout=30)
                response.raise_for_status()
                payload = response.json()

                for item in payload.get("upcoming", []) + payload.get("past", []):
                    record = _record_from_item(item)
                    if record is not None:
                        records.append(record)

                pagination = payload.get("pagination") or {}
                next_path = pagination.get("nextPageUrl") if pagination.get("nextPage") else None
                if next_path:
                    separator = "&" if "?" in next_path else "?"
                    page_url = urljoin(SOURCE_URL, f"{next_path}{separator}format=json")
                else:
                    page_url = None

        log_message(
            "Scraped SFCO events",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    TheSfcoCrawler().run()


if __name__ == "__main__":
    main()
