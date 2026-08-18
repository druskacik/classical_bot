import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Chamber Music International"
SOURCE_URL = "https://www.chambermusicinternational.org/"
SERVICES_URL = f"{SOURCE_URL.rstrip('/')}/_api/bookings/v2/services/query"
TOKENS_URL = f"{SOURCE_URL.rstrip('/')}/_api/v1/access-tokens"
BOOKINGS_APP_ID = "13d21c63-b5ec-5912-8397-c3a5ddb27a97"
LOCAL_TIMEZONE = ZoneInfo("America/Chicago")
PAGE_SIZE = 100

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
}


def _clean(value):
    if not value:
        return ""
    return re.sub(r"[ \t]+", " ", str(value).replace("\xa0", " ")).strip()


def _location(service):
    for location in service.get("locations") or []:
        address = location.get("calculatedAddress") or (location.get("custom") or {}).get("address") or {}
        text = _clean(address.get("formattedAddress") or address.get("addressLine"))
        parts = [_clean(part) for part in text.split(",")]
        parts = [part for part in parts if part]
        if len(parts) >= 2:
            return parts[0], parts[1]
    return None, None


def _start(service):
    value = (service.get("schedule") or {}).get("firstSessionStart")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(LOCAL_TIMEZONE)
    except (TypeError, ValueError, OverflowError):
        return None


def _record(service):
    title = _clean(service.get("name"))
    url = ((service.get("urls") or {}).get("servicePage") or {}).get("url")
    start = _start(service)
    venue, city = _location(service)
    if not all((title, url, start, venue, city)):
        return None
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": start.strftime("%H:%M"),
        "venue": venue,
        "city": city,
        "country_code": "US",
        "description": _clean(service.get("description")) or None,
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


class ChamberMusicInternationalCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="chambermusicinternational_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        token_response = session.get(TOKENS_URL, timeout=45)
        token_response.raise_for_status()
        app = (token_response.json().get("apps") or {}).get(BOOKINGS_APP_ID) or {}
        authorization = app.get("instance")
        if not authorization:
            raise RuntimeError("Wix Bookings access token is unavailable")

        api_headers = {"Authorization": authorization, "Content-Type": "application/json"}
        services = []
        offset = 0
        while True:
            payload = {
                "query": {
                    "sort": [
                        {"fieldName": "category.sortOrder", "order": "ASC"},
                        {"fieldName": "sortOrder", "order": "ASC"},
                    ],
                    "paging": {"limit": PAGE_SIZE, "offset": offset},
                    "filter": {
                        "appId": BOOKINGS_APP_ID,
                        "$or": [{"hidden": False}, {"hidden": {"$exists": False}}],
                    },
                }
            }
            response = session.post(SERVICES_URL, headers=api_headers, json=payload, timeout=45)
            response.raise_for_status()
            data = response.json()
            page = data.get("services") or []
            services.extend(page)
            paging = data.get("pagingMetadata") or {}
            if not paging.get("hasNext") or not page:
                break
            offset += len(page)

        records = []
        skipped = 0
        for service in services:
            record = _record(service)
            if record:
                records.append(record)
            else:
                skipped += 1
        if skipped:
            log_message(
                "Skipped services missing required concert fields",
                event="crawler_records_skipped",
                level="warning",
                url=SERVICES_URL,
                record_count=skipped,
            )
        log_message(
            "Wix concert services parsed",
            event="crawler_scrape_completed",
            url=SERVICES_URL,
            record_count=len(records),
        )
        return sorted(records, key=lambda item: (item["date"], item["time_from"], item["title"]))


def main():
    ChamberMusicInternationalCrawler().run()


if __name__ == "__main__":
    main()
