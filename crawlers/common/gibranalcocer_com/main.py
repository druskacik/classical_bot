from datetime import datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Gibran Alcocer"
SOURCE_URL = "https://gibranalcocer.com/"
EVENTS_URL = "https://rest.bandsintown.com/V4/artists/id_15579686/events/"

COUNTRY_CODES = {
    "Austria": "AT",
    "Canada": "CA",
    "Denmark": "DK",
    "France": "FR",
    "Germany": "DE",
    "Italy": "IT",
    "Netherlands": "NL",
    "Poland": "PL",
    "United Kingdom": "GB",
    "United States": "US",
}


class GibranAlcocerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="gibranalcocer_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert feed", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(
            EVENTS_URL,
            params={"app_id": "js_gibranalcocer.com", "date": "all"},
            timeout=30,
        )
        response.raise_for_status()

        records = []
        for event in response.json():
            venue = event.get("venue") or {}
            country_code = COUNTRY_CODES.get(venue.get("country"))
            if not country_code or not venue.get("city") or not venue.get("name"):
                continue

            starts_at = event.get("starts_at") or event.get("datetime")
            if not starts_at:
                continue
            try:
                start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            except ValueError:
                continue

            event_id = event.get("id")
            if not event_id:
                continue

            records.append(
                {
                    "title": event.get("title") or SOURCE,
                    "date": start.date().isoformat(),
                    "url": f"https://www.bandsintown.com/e/{event_id}",
                    "time_from": start.time().replace(tzinfo=None).isoformat(timespec="minutes"),
                    "time_to": None,
                    "venue": venue["name"].strip(),
                    "city": venue["city"].strip(),
                    "country_code": country_code,
                    "description": (event.get("description") or "").strip() or None,
                }
            )

        log_message(
            "Concert feed parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
            url=EVENTS_URL,
        )
        return records


def main():
    GibranAlcocerCrawler().run()


if __name__ == "__main__":
    main()
