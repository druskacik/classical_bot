from datetime import datetime
import html

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Southeastern Ohio Symphony Orchestra"
SOURCE_URL = "http://seoso.org/"
API_URL = "http://seoso.org/wp-json/tribe/events/v1/events"


def _plain_text(value: str | None) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    return text or None


class SeosoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="seoso_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        session = requests.Session()
        session.headers["User-Agent"] = "classical-bot concert crawler"

        # The endpoint rejects very large date spans, so cover its full useful
        # calendar range with non-overlapping windows that it accepts.
        horizon_year = datetime.now().year + 10
        for start_year in range(2000, horizon_year + 1, 20):
            page = 1
            total_pages = 1
            end_year = min(start_year + 19, horizon_year)
            while page <= total_pages:
                params = {
                    "page": page,
                    "per_page": 50,
                    "start_date": f"{start_year}-01-01 00:00:00",
                    "end_date": f"{end_year}-12-31 23:59:59",
                    "status": "publish",
                }
                log_message(
                    "Fetching events API page",
                    event="crawler_url_fetch",
                    url=API_URL,
                    page=page,
                    start_year=start_year,
                    end_year=end_year,
                )
                response = session.get(API_URL, params=params, timeout=30)
                response.raise_for_status()
                payload = response.json()
                total_pages = int(payload.get("total_pages") or 1)

                for event in payload.get("events", []):
                    venue = event.get("venue") or {}
                    venue_name = _plain_text(venue.get("venue"))
                    city = _plain_text(venue.get("city"))
                    title = html.unescape(_plain_text(event.get("title")) or "")
                    url = event.get("url")
                    start_value = event.get("start_date")

                    if not all((title, url, start_value, venue_name, city)):
                        log_message(
                            "Skipping event with incomplete required fields",
                            event="crawler_record_skipped",
                            url=url,
                            event_id=event.get("id"),
                        )
                        continue

                    try:
                        start = datetime.fromisoformat(start_value)
                        end_value = event.get("end_date")
                        end = datetime.fromisoformat(end_value) if end_value else None
                    except (TypeError, ValueError) as error:
                        log_message(
                            "Skipping event with invalid date",
                            event="crawler_record_skipped",
                            url=url,
                            event_id=event.get("id"),
                            error_type=type(error).__name__,
                            error_message=str(error),
                        )
                        continue

                    records.append(
                        {
                            "title": title,
                            "date": start.date().isoformat(),
                            "url": url,
                            "time_from": start.time().isoformat(timespec="minutes"),
                            "time_to": end.time().isoformat(timespec="minutes") if end else None,
                            "venue": venue_name,
                            "city": city,
                            "description": _plain_text(event.get("description")),
                        }
                    )

                page += 1

        return records


def main():
    SeosoOrgCrawler().run()


if __name__ == "__main__":
    main()
