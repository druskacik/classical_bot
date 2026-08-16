import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Centro Nacional de las Artes Delia Zapata Olivella"
SOURCE_URL = "https://eneldelia.gov.co/"
API_URL = "https://eneldelia.gov.co/wp-json/tribe/events/v1/events"
DEFAULT_VENUE = "Centro Nacional de las Artes Delia Zapata Olivella"
DEFAULT_CITY = "Bogotá"
PAGE_SIZE = 50


def _clean_text(value):
    if not value:
        return None
    text = BeautifulSoup(html.unescape(value), "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _extract_venue(event):
    venue = event.get("venue")
    if isinstance(venue, dict):
        name = _clean_text(venue.get("venue"))
        if name:
            return name

    soup = BeautifulSoup(event.get("description") or "", "html.parser")
    for element in soup.find_all(["p", "div", "li"]):
        text = element.get_text(" ", strip=True)
        match = re.match(r"(?i)^\s*lugar\s*:\s*(.+?)\s*$", text)
        if match:
            candidate = match.group(1).strip(" .-–")
            if candidate:
                return candidate
    return DEFAULT_VENUE


def _event_record(event):
    title = _clean_text(event.get("title"))
    url = event.get("url")
    start_value = event.get("start_date")
    if not title or not url or not start_value:
        return None

    try:
        start = datetime.strptime(start_value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None

    time_from = None if event.get("all_day") else start.strftime("%H:%M:%S")
    time_to = None
    end_value = event.get("end_date")
    if end_value and not event.get("all_day"):
        try:
            end = datetime.strptime(end_value, "%Y-%m-%d %H:%M:%S")
            time_to = end.strftime("%H:%M:%S")
        except (TypeError, ValueError):
            pass

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": time_from,
        "time_to": time_to,
        "venue": _extract_venue(event),
        "city": DEFAULT_CITY,
        "description": _clean_text(event.get("description")),
    }


class EnElDeliaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="eneldelia_gov_co",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="CO",
        upload_target="potential",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        page = 1
        params = {
            "per_page": PAGE_SIZE,
            "start_date": "2023-01-01 00:00:00",
            "end_date": "2100-12-31 23:59:59",
            "status": "publish",
        }

        while True:
            log_message(
                "Fetching events API page",
                event="crawler_url_fetch",
                url=API_URL,
                page=page,
            )
            response = requests.get(API_URL, params={**params, "page": page}, timeout=60)
            response.raise_for_status()
            payload = response.json()

            for event in payload.get("events", []):
                record = _event_record(event)
                if record:
                    records.append(record)

            total_pages = int(payload.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1

        log_message(
            "Events API scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    EnElDeliaCrawler().run()


if __name__ == "__main__":
    main()
