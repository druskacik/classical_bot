import re
from datetime import datetime
from html import unescape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.annafedorova.com/"
SOURCE = "Anna Fedorova"
ACCESS_TOKENS_URL = f"{SOURCE_URL}_api/v1/access-tokens"
EVENTS_URL = f"{SOURCE_URL}_api/wix-one-events-server/web/paginated-events/viewer"
EVENTS_APP_ID = "140603ad-af8d-84a5-2c80-a0f60cb47351"
PAGE_SIZE = 100

# These are the two first-party Wix Events widgets used by the public upcoming
# and past-event pages. Wix identifies their feeds with stable numeric values.
FEEDS = (
    {"filter": 1, "filterType": 2, "compId": "comp-l3wzross"},
    {"filter": 2, "filterType": 3, "compId": "comp-l5s5i92n"},
)


def _clean_html(value):
    if not value:
        return None
    text = BeautifulSoup(unescape(value), "html.parser").get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text or None


def _local_start(scheduling):
    config = scheduling.get("config", {})
    raw_start = config.get("startDate")
    if not raw_start:
        return None
    start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
    timezone_name = config.get("timeZoneId")
    if timezone_name:
        try:
            start = start.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            pass
    return start


class AnnaFedorovaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="annafedorova_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def _access_token(self, session):
        response = session.get(ACCESS_TOKENS_URL, timeout=30)
        response.raise_for_status()
        return response.json()["apps"][EVENTS_APP_ID]["instance"]

    def _fetch_feed(self, session, headers, feed):
        events = []
        offset = 0
        while True:
            params = {
                **feed,
                "offset": offset,
                "limit": PAGE_SIZE,
                "sortOrder": 0,
                "byEventId": "false",
                "members": "true",
                "paidPlans": "false",
                "locale": "en-us",
                "fetchBadges": "true",
                "draft": "false",
            }
            response = session.get(EVENTS_URL, params=params, headers=headers, timeout=60)
            response.raise_for_status()
            payload = response.json()
            page = payload.get("events", [])
            events.extend(page)
            if not payload.get("hasMore") or not page:
                break
            offset += len(page)
        return events

    def _record(self, event):
        title = (event.get("title") or "").strip()
        slug = (event.get("slug") or "").strip()
        location = event.get("location") or {}
        venue = (location.get("name") or "").strip()
        full_address = location.get("fullAddress") or {}
        country_code = (full_address.get("country") or "").strip().upper()

        # Newer listings advertise the city before the first pipe; older ones
        # often use a repertoire-only title and need Wix's address city.
        title_location = title.split("|", 1)[0]
        if "|" in title and "," in title_location:
            city = title_location.split(",", 1)[0].strip()
        else:
            city = (full_address.get("city") or "").strip()
        start = _local_start(event.get("scheduling") or {})
        if not all((title, slug, venue, city, country_code, start)):
            return None
        if not re.fullmatch(r"[A-Z]{2}", country_code):
            return None
        if venue.casefold() == city.casefold():
            return None

        description_parts = [
            _clean_html(event.get("description")),
            _clean_html(event.get("about")),
        ]
        description = "\n".join(part for part in description_parts if part) or None
        return {
            "title": title,
            "date": start.date().isoformat(),
            "url": f"{SOURCE_URL}event-details/{slug}",
            "time_from": start.strftime("%H:%M"),
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "classical-bot/1.0"})
        token = self._access_token(session)
        headers = {"authorization": token, "x-wix-brand": "wix"}

        events = []
        for feed in FEEDS:
            events.extend(self._fetch_feed(session, headers, feed))

        records = [record for event in events if (record := self._record(event))]
        log_message(
            "Anna Fedorova calendar scraped",
            event="crawler_scrape_completed",
            record_count=len(records),
            raw_event_count=len(events),
        )
        return records


def main():
    AnnaFedorovaCrawler().run()


if __name__ == "__main__":
    main()
