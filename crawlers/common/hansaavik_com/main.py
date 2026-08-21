import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Hans Christian Aavik"
SOURCE_URL = "https://www.hansaavik.com/"
EVENTS_APP_ID = "140603ad-af8d-84a5-2c80-a0f60cb47351"
EVENTS_API = (
    "https://www.hansaavik.com/_api/wix-one-events-server/"
    "web/paginated-events/viewer"
)
PAGE_SIZE = 100

COUNTRY_CODES = {
    "denmark": "DK",
    "estonia": "EE",
    "finland": "FI",
    "germany": "DE",
    "italy": "IT",
    "romania": "RO",
    "uk": "GB",
    "united kingdom": "GB",
}


def _clean(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _country_code(location):
    full_address = location.get("fullAddress") or {}
    value = _clean(full_address.get("country"))
    if value and re.fullmatch(r"[A-Za-z]{2}", value):
        return "GB" if value.upper() == "UK" else value.upper()

    address = _clean(location.get("address"))
    if not address:
        return None
    country = address.rsplit(",", 1)[-1].strip().lower()
    return COUNTRY_CODES.get(country)


def _city(location):
    full_address = location.get("fullAddress") or {}
    city = _clean(full_address.get("city"))
    if city:
        return city

    address = _clean(location.get("address"))
    if not address or "," not in address:
        return None
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) < 2:
        return None

    # Older Wix records lack structured address fields. The city is normally
    # the penultimate comma-delimited component, optionally after a postcode.
    candidate = parts[-2]
    candidate = re.sub(r"^\d{4,6}(?:-[A-Z0-9]+)?\s+", "", candidate)
    candidate = re.sub(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\s+", "", candidate, flags=re.I)
    candidate = _clean(candidate)
    if candidate and not re.fullmatch(r"\d+(?:\s+[A-Za-z -]+)?", candidate):
        return candidate

    name = _clean(location.get("name"))
    if name and name.lower() not in {"location is tbd", "location is private"}:
        return name
    return None


def _local_datetime(value, timezone_name):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return instant.astimezone(ZoneInfo(timezone_name))


class HansaavikCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="hansaavik_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def _fetch_page(self, session, token, offset):
        params = {
            "offset": offset,
            "filter": 1,
            "filterType": 1,
            "sortOrder": 0,
            "limit": PAGE_SIZE,
            "locale": "en-us",
            "fetchBadges": "true",
            "draft": "false",
            "compId": "comp-j4cgg1gh",
        }
        response = session.get(
            EVENTS_API,
            params=params,
            headers={"authorization": token, "x-wix-brand": "wix"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def scrape(self):
        session = requests.Session()
        log_message("Fetching source page", event="crawler_url_fetch", url=SOURCE_URL)
        response = session.get(SOURCE_URL, timeout=30)
        response.raise_for_status()

        token_url = f"{SOURCE_URL.rstrip('/')}/_api/v1/access-tokens"
        log_message("Fetching Wix access token", event="crawler_url_fetch", url=token_url)
        token_response = session.get(token_url, timeout=30)
        token_response.raise_for_status()
        token = token_response.json()["apps"][EVENTS_APP_ID]["instance"]

        events = []
        offset = 0
        while True:
            page = self._fetch_page(session, token, offset)
            page_events = page.get("events") or []
            events.extend(page_events)
            if not page.get("hasMore") or not page_events:
                break
            offset += len(page_events)

        records = []
        for event in events:
            location = event.get("location") or {}
            venue = _clean(location.get("name"))
            city = _city(location)
            country_code = _country_code(location)
            if (
                not venue
                or venue.lower() in {
                    "location is tbd",
                    "location is private",
                    "this is a private concert",
                    "the location is requested to be private",
                }
                or venue.startswith(("http://", "https://"))
                or not city
                or not country_code
                or venue.casefold() == city.casefold()
            ):
                continue

            schedule = (event.get("scheduling") or {}).get("config") or {}
            start_value = schedule.get("startDate")
            timezone_name = schedule.get("timeZoneId")
            title = _clean(event.get("title"))
            if not start_value or not timezone_name or not title:
                continue
            try:
                start = _local_datetime(start_value, timezone_name)
                end = None
                if schedule.get("endDate") and not schedule.get("endDateHidden"):
                    end = _local_datetime(schedule["endDate"], timezone_name)
            except (ValueError, TypeError, KeyError):
                continue

            registration = (event.get("registration") or {}).get("external") or {}
            url = _clean(registration.get("registration"))
            if not url:
                url = f"{SOURCE_URL.rstrip('/')}/event-details/{event['slug']}"

            description_parts = [
                part for part in (_clean(event.get("description")), _clean(event.get("about")))
                if part
            ]
            records.append(
                {
                    "title": title,
                    "date": start.date().isoformat(),
                    "url": url,
                    "time_from": start.time().replace(tzinfo=None).isoformat(timespec="minutes"),
                    "time_to": (
                        end.time().replace(tzinfo=None).isoformat(timespec="minutes")
                        if end and end.date() == start.date()
                        else None
                    ),
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": "\n\n".join(description_parts) or None,
                }
            )

        log_message(
            "Parsed Wix events",
            event="crawler_parse_completed",
            record_count=len(records),
            discovered_count=len(events),
        )
        return records


def main():
    HansaavikCrawler().run()


if __name__ == "__main__":
    main()
