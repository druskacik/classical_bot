import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.courtneycaston.com/"
SOURCE = "Courtney Caston"
EVENT_SITEMAP_URL = f"{SOURCE_URL}event-pages-sitemap.xml"
EVENTS_APP_ID = "140603ad-af8d-84a5-2c80-a0f60cb47351"


def _warmup_data(response: requests.Response) -> dict:
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.find("script", id="wix-warmup-data")
    if node is None or not node.string:
        raise ValueError("Wix warmup data was not found")
    return json.loads(node.string)


def _app_states(data: dict) -> list[dict]:
    app_data = data.get("appsWarmupData", {}).get(EVENTS_APP_ID, {})
    return [state for state in app_data.values() if isinstance(state, dict)]


def _detail_event(data: dict) -> dict | None:
    for state in _app_states(data):
        event = state.get("event", {}).get("event")
        if isinstance(event, dict):
            return event
    return None


def _event_urls(response: requests.Response) -> list[str]:
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "xml")
    return [
        node.get_text(strip=True)
        for node in soup.find_all("loc")
        if "/event-details/" in node.get_text()
    ]


def _rich_text(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(filter(None, (_rich_text(item).strip() for item in node)))
    if not isinstance(node, dict):
        return ""

    text = node.get("text", "")
    children = _rich_text(node.get("nodes", []))
    return "\n".join(part for part in (text.strip(), children.strip()) if part)


def _description(event: dict) -> str | None:
    parts = []
    for value in (event.get("description"), event.get("about")):
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    long_description = _rich_text(event.get("longDescription", {})).strip()
    if long_description:
        parts.append(long_description)
    return "\n\n".join(dict.fromkeys(parts)) or None


def _local_start(event: dict) -> datetime:
    scheduling = event.get("scheduling", {}).get("config", {})
    start = scheduling.get("startDate")
    timezone = scheduling.get("timeZoneId")
    if not start or not timezone:
        raise ValueError("Event has no definite start date or timezone")
    return datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(ZoneInfo(timezone))


class CourtneyCastonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="courtneycaston_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalBot/1.0)"
        log_message("Fetching event sitemap", event="crawler_url_fetch", url=EVENT_SITEMAP_URL)
        urls = _event_urls(session.get(EVENT_SITEMAP_URL, timeout=30))

        records = []
        for url in urls:
            try:
                log_message("Fetching event detail", event="crawler_url_fetch", url=url)
                event = _detail_event(_warmup_data(session.get(url, timeout=30)))
            except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
                log_message(
                    "Event detail fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if not event:
                continue

            location = event.get("location") or {}
            full_address = location.get("fullAddress") or {}
            title = (event.get("title") or "").strip()
            venue = (location.get("name") or "").strip()
            city = (full_address.get("city") or "").strip()
            country_code = (full_address.get("country") or "").strip().upper()
            if not all((title, venue, city, country_code)) or venue.casefold() == city.casefold():
                continue
            try:
                start = _local_start(event)
            except (ValueError, TypeError, KeyError):
                continue

            schedule_tbd = event.get("scheduling", {}).get("config", {}).get("scheduleTbd", False)
            records.append(
                {
                    "title": title,
                    "date": start.date().isoformat(),
                    "url": url,
                    "time_from": None if schedule_tbd else start.strftime("%H:%M"),
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": _description(event),
                }
            )

        log_message("Event schedule parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    CourtneyCastonCrawler().run()


if __name__ == "__main__":
    main()
