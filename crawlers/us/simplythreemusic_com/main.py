from datetime import datetime

import requests

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Simply Three"
SOURCE_URL = "https://simplythreemusic.com/"
EVENTS_API_URL = "https://rest.bandsintown.com/artists/Simply%20Three/events"
API_PARAMS = {"app_id": "squarespace-simplythree"}
COUNTRY_CODES = {
    "Canada": "CA",
    "Mexico": "MX",
    "United States": "US",
    "USA": "US",
}


def _parse_time(event: dict, field: str) -> str | None:
    value = event.get(field)
    if not value or event.get("datetime_display_rule") == "date":
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return None


def _event_to_record(event: dict) -> dict | None:
    venue = event.get("venue") or {}
    city = (venue.get("city") or "").strip()
    venue_name = (venue.get("name") or "").strip()
    if event.get("id") == "108362578" and "MesaArtsCenter.com" in (event.get("description") or ""):
        venue_name = "Mesa Arts Center"
    country_code = COUNTRY_CODES.get((venue.get("country") or "").strip())
    date_value = event.get("datetime") or event.get("starts_at")

    if not date_value or not city or not venue_name or not country_code:
        return None

    try:
        event_date = datetime.fromisoformat(date_value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None

    event_id = str(event.get("id") or "").strip()
    url = (event.get("url") or "").strip()
    if not event_id or not url:
        return None

    title = (event.get("title") or "").strip() or SOURCE
    description = (event.get("description") or "").strip() or None

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": _parse_time(event, "starts_at") or _parse_time(event, "datetime"),
        "time_to": _parse_time(event, "ends_at"),
        "venue": venue_name,
        "city": city,
        "country_code": country_code,
        "description": description,
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


class SimplyThreeMusicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="simplythreemusic_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["url", "date"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update({"User-Agent": "classical-crawler/1.0"})
        records = []

        for date_range in ("past", "upcoming"):
            params = {**API_PARAMS, "date": date_range}
            log_message(
                "Fetching Simply Three events",
                event="crawler_url_fetch",
                url=EVENTS_API_URL,
                date_range=date_range,
            )
            response = session.get(EVENTS_API_URL, params=params, timeout=30)
            response.raise_for_status()
            events = response.json()
            if not isinstance(events, list):
                raise ValueError("Bandsintown events response is not a list")

            for event in events:
                record = _event_to_record(event)
                if record is not None:
                    records.append(record)

        log_message(
            "Simply Three events parsed",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    SimplyThreeMusicComCrawler().run()


if __name__ == "__main__":
    main()
