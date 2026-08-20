from datetime import datetime
from html import unescape
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Alice Sara Ott"
SOURCE_URL = "https://alicesaraott.com/"
COLLECTION_URL = urljoin(SOURCE_URL, "schedule")
SITE_TIMEZONE = ZoneInfo("Europe/Berlin")

COUNTRY_CODES = {
    "Armenia": "AM",
    "Austria": "AT",
    "Belgium": "BE",
    "China": "CN",
    "Croatia": "HR",
    "Czech Republic": "CZ",
    "Denmark": "DK",
    "Estonia": "EE",
    "France": "FR",
    "Germany": "DE",
    "Hungary": "HU",
    "Iceland": "IS",
    "Ireland": "IE",
    "Italy": "IT",
    "Japan": "JP",
    "Korea": "KR",
    "Luxembourg": "LU",
    "MO": "MO",
    "Netherlands": "NL",
    "Norway": "NO",
    "Poland": "PL",
    "Republic of Korea": "KR",
    "Scotland": "GB",
    "Serbia": "RS",
    "Slovenia": "SI",
    "Spain": "ES",
    "Sweden": "SE",
    "Switzerland": "CH",
    "The Netherlands": "NL",
    "Turkey": "TR",
    "UAE": "AE",
    "UK": "GB",
    "USA": "US",
    "United Kingdom": "GB",
    "United States": "US",
}


def _json_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["format"] = "json"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _text_from_html(value: str | None) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", unescape(text)).strip()
    return text or None


def _place_from_title(title: str) -> tuple[str, str, str] | None:
    location, separator, venue = title.partition(" - ")
    if not separator or not venue.strip() or "," not in location:
        return None
    city, country = (part.strip() for part in location.rsplit(",", 1))
    country_code = COUNTRY_CODES.get(country)
    if not city or not country_code:
        return None
    return city, country_code, venue.strip()


def _event_record(item: dict) -> dict | None:
    title = re.sub(r"\s+", " ", item.get("title", "")).strip()
    place = _place_from_title(title)
    if not title or not place or not item.get("urlId") or not item.get("startDate"):
        return None

    city, country_code, venue = place
    start = datetime.fromtimestamp(item["startDate"] / 1000, tz=SITE_TIMEZONE)
    end_ms = item.get("endDate")
    end = datetime.fromtimestamp(end_ms / 1000, tz=SITE_TIMEZONE) if end_ms else None
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": urljoin(COLLECTION_URL + "/", item["urlId"]),
        "time_from": start.strftime("%H:%M:%S"),
        "time_to": end.strftime("%H:%M:%S") if end else None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": _text_from_html(item.get("body")),
    }


class AliceSaraOttCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="alicesaraott_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        seen_ids = set()
        next_url = _json_url(COLLECTION_URL)

        while next_url:
            log_message("Fetching concert page", event="crawler_url_fetch", url=next_url)
            response = requests.get(next_url, timeout=30)
            response.raise_for_status()
            payload = response.json()

            for item in [*payload.get("upcoming", []), *payload.get("past", [])]:
                item_id = item.get("id")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                record = _event_record(item)
                if record is not None:
                    records.append(record)

            next_path = payload.get("pagination", {}).get("nextPageUrl")
            next_url = _json_url(urljoin(SOURCE_URL, next_path)) if next_path else None

        log_message(
            "Concert catalogue scraped",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    AliceSaraOttCrawler().run()


if __name__ == "__main__":
    main()
