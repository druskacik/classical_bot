from datetime import datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.albertcanosmit.com/"
SOURCE = "Albert Cano Smit"
ACCESS_TOKEN_URL = f"{SOURCE_URL}_api/v1/access-tokens"
EVENTS_URL = (
    f"{SOURCE_URL}_api/wix-one-events-server/web/paginated-events/viewer"
)
EVENTS_APP_ID = "140603ad-af8d-84a5-2c80-a0f60cb47351"
PAGE_SIZE = 100
TIMEOUT = 30


class AlbertCanoSmitCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="albertcanosmit_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ClassicalBot/1.0"})

    def _authorization_token(self):
        log_message(
            "Fetching Wix access token",
            event="crawler_url_fetch",
            url=ACCESS_TOKEN_URL,
        )
        response = self.session.get(ACCESS_TOKEN_URL, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()["apps"][EVENTS_APP_ID]["instance"]

    def _fetch_feed(self, authorization, event_filter, filter_type, comp_id):
        records = []
        offset = 0

        while True:
            params = {
                "offset": offset,
                "filter": event_filter,
                "byEventId": "false",
                "paidPlans": "false",
                "locale": "en-us",
                "filterType": filter_type,
                "sortOrder": 0,
                "limit": PAGE_SIZE,
                "fetchBadges": "true",
                "draft": "false",
                "compId": comp_id,
            }
            log_message(
                "Fetching Wix events page",
                event="crawler_url_fetch",
                url=EVENTS_URL,
                offset=offset,
                event_filter=event_filter,
            )
            response = self.session.get(
                EVENTS_URL,
                params=params,
                headers={"Authorization": authorization, "X-Wix-Brand": "wix"},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            page_events = payload.get("events", [])
            event_dates = payload.get("dates", {}).get("events", {})

            for event in page_events:
                record = self._parse_event(event, event_dates.get(event["id"], {}))
                if record is not None:
                    records.append(record)

            if not payload.get("hasMore") or not page_events:
                break
            offset += len(page_events)

        return records

    @staticmethod
    def _parse_event(event, dates):
        location = event.get("location") or {}
        full_address = location.get("fullAddress") or {}
        local_start = dates.get("startDateISOFormatNotUTC")
        venue = (location.get("name") or "").strip()
        city = (full_address.get("city") or "").strip()
        country_code = (full_address.get("country") or "").strip().upper()

        if (
            not local_start
            or not venue
            or not city
            or venue.casefold() == city.casefold()
            or len(country_code) != 2
        ):
            log_message(
                "Skipping event with incomplete date or location",
                event="crawler_record_skipped",
                url=f'{SOURCE_URL}event-details/{event.get("slug", "")}',
            )
            return None

        start = datetime.fromisoformat(local_start)
        description_parts = [event.get("description"), event.get("about")]
        description = "\n\n".join(
            part.strip() for part in description_parts if part and part.strip()
        ) or None
        slug = event.get("slug")
        title = (event.get("title") or "").strip()
        if not slug or not title:
            return None

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
        authorization = self._authorization_token()
        upcoming = self._fetch_feed(authorization, 1, 2, "comp-l2ejrp98")
        past = self._fetch_feed(authorization, 2, 3, "comp-lm37xhte")
        records = upcoming + past
        log_message(
            "Collected Wix events",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    AlbertCanoSmitCrawler().run()


if __name__ == "__main__":
    main()
