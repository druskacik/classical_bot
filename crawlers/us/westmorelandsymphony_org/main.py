from datetime import datetime
import html
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Westmoreland Symphony Orchestra"
SOURCE_URL = "https://westmorelandsymphony.org/"
API_URL = "https://westmorelandsymphony.org/wp-json/tribe/events/v1/events"

# The API omits the city from an older Museum event, but supplies its 15601
# postal code.  That ZIP is in Greensburg, where the named museum is located.
CITY_BY_POSTAL_CODE = {"15601": "Greensburg"}


def _clean_text(value):
    if value is None:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text("\n", strip=True)
    text = html.unescape(text).replace("\xa0", " ").replace("\u200b", "")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned or None


def _valid_event_url(value):
    if not value:
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"westmorelandsymphony.org", "www.westmorelandsymphony.org"}
        and parsed.path.startswith("/event/")
    )


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return None


class WestmorelandSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="westmorelandsymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def _fetch_page(self, page):
        params = {
            "page": page,
            "per_page": 50,
            "start_date": "1900-01-01",
            "end_date": "2100-12-31",
        }
        log_message(
            "Fetching event API page",
            event="crawler_url_fetch",
            url=API_URL,
            page=page,
        )
        response = requests.get(
            API_URL,
            params=params,
            timeout=45,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
            },
        )
        response.raise_for_status()
        return response.json()

    def scrape(self):
        records = []
        page = 1

        while True:
            payload = self._fetch_page(page)
            events = payload.get("events") or []
            for event in events:
                title = _clean_text(event.get("title"))
                url = event.get("url")
                start = _parse_datetime(event.get("start_date"))
                venue_data = event.get("venue") or {}
                venue = _clean_text(venue_data.get("venue"))
                city = _clean_text(venue_data.get("city"))
                if not city:
                    city = CITY_BY_POSTAL_CODE.get(str(venue_data.get("zip") or "").strip())

                if not title or not _valid_event_url(url) or start is None or not venue or not city:
                    log_message(
                        "Skipping event missing required fields",
                        event="crawler_record_skipped",
                        url=url or API_URL,
                        error_type="MissingRequiredField",
                        error_message="title, URL, date, venue, or city is unavailable",
                    )
                    continue

                all_day = bool(event.get("all_day"))
                end = _parse_datetime(event.get("end_date"))
                time_from = None if all_day else start.strftime("%H:%M:%S")
                time_to = None
                if not all_day and end is not None and end.date() == start.date():
                    time_to = end.strftime("%H:%M:%S")

                records.append(
                    {
                        "title": title,
                        "date": start.date().isoformat(),
                        "url": url,
                        "time_from": time_from,
                        "time_to": time_to,
                        "venue": venue,
                        "city": city,
                        "description": _clean_text(event.get("description")),
                    }
                )

            total_pages = int(payload.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1

        log_message(
            "Event API parsed",
            event="crawler_scrape_completed",
            url=API_URL,
            record_count=len(records),
        )
        return records


def main():
    WestmorelandSymphonyCrawler().run()


if __name__ == "__main__":
    main()
