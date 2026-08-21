import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.katiagocs.com/"
SOURCE = "Kati Agócs"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar")
SITE_TIMEZONE = ZoneInfo("America/New_York")

CANADIAN_REGIONS = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "ONT",
    "PE", "PEI", "PQ", "QC", "SK", "YT",
}
US_REGIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}
COUNTRY_NAMES = {
    "australia": "AU", "austria": "AT", "canada": "CA",
    "finland": "FI", "france": "FR", "germany": "DE", "germamy": "DE",
    "hungary": "HU", "ireland": "IE", "italy": "IT",
    "netherlands": "NL", "northern ireland": "GB", "norway": "NO",
    "poland": "PL", "slovenia": "SI",
    "sweden": "SE", "switzerland": "CH", "united kingdom": "GB",
    "uk": "GB", "united states": "US", "usa": "US",
}
VENUE_WORDS = re.compile(
    r"\b(?:arts? cent(?:er|re)|cent(?:er|re)|atelje|auditorium|bain mathieu|"
    r"cathedral|chapel|church|college|"
    r"conservator(?:y|ium)|foundation|gallery|hall|house|kirche|kunstgarten|"
    r"bookstore|building|club|garden|museum|opera|peterloon|recital|room|"
    r"school|sawdust|space|stage|studio|theat(?:er|re)|university|villa)\b",
    re.IGNORECASE,
)


def _clean_lines(html: str) -> list[str]:
    text = BeautifulSoup(html or "", "html.parser").get_text("\n")
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()
            if line.strip()]


def _country_for_tail(tail: str) -> str | None:
    normalized = re.sub(r"[^A-Za-z ]", "", tail).strip().casefold()
    if normalized in COUNTRY_NAMES:
        return COUNTRY_NAMES[normalized]
    region = re.sub(r"[^A-Za-z]", "", tail).upper()
    if region in CANADIAN_REGIONS:
        return "CA"
    if region in US_REGIONS:
        return "US"
    return None


def _parse_location(lines: list[str]) -> tuple[str | None, str | None, str | None]:
    """Return venue, city and country from the last credible location line."""
    for index in range(len(lines) - 1, -1, -1):
        line = re.sub(r"\s+\d{4,6}$", "", lines[index]).strip()
        parts = [part.strip() for part in line.split(",") if part.strip()]
        if len(parts) < 2:
            continue
        country_code = _country_for_tail(parts[-1])
        if country_code is None:
            continue

        city = parts[-2]
        if not city or re.search(r"\d|https?://", city):
            continue

        venue = ", ".join(parts[:-2]).strip() or None
        if venue is None and index > 0 and VENUE_WORDS.search(lines[index - 1]):
            venue = lines[index - 1]
        if venue:
            return venue, city, country_code
    return None, None, None


def _event_record(item: dict) -> dict | None:
    title = re.sub(r"\s+", " ", item.get("title", "")).strip()
    full_url = item.get("fullUrl")
    start_ms = item.get("startDate")
    if not title or not full_url or not isinstance(start_ms, (int, float)):
        return None

    lines = _clean_lines(item.get("body") or item.get("excerpt") or "")
    venue, city, country_code = _parse_location(lines)
    if not venue or not city or not country_code:
        log_message(
            "Skipping calendar event with unresolved location",
            event="crawler_event_skipped",
            url=urljoin(SOURCE_URL, full_url),
            error_type="UnresolvedLocation",
        )
        return None

    start = datetime.fromtimestamp(start_ms / 1000, tz=SITE_TIMEZONE)
    end_ms = item.get("endDate")
    end = (datetime.fromtimestamp(end_ms / 1000, tz=SITE_TIMEZONE)
           if isinstance(end_ms, (int, float)) else None)
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": urljoin(SOURCE_URL, full_url),
        "time_from": start.strftime("%H:%M"),
        "time_to": end.strftime("%H:%M") if end else None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": "\n".join(lines) or None,
    }


class KatiaGocsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="katiagocs_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self):
        self.session = requests.Session()

    def _get_json(self, params: dict) -> dict:
        log_message(
            "Fetching calendar feed",
            event="crawler_url_fetch",
            url=CALENDAR_URL,
        )
        response = self.session.get(CALENDAR_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _season_items(self, category: str) -> list[dict]:
        params = {"category": category, "format": "json", "view": "list"}
        items: list[dict] = []
        seen: set[str] = set()
        while True:
            payload = self._get_json(params)
            page = payload.get("upcoming", []) + payload.get("past", [])
            new_items = [item for item in page if item.get("id") not in seen]
            items.extend(new_items)
            seen.update(item["id"] for item in new_items if item.get("id"))
            if len(page) < 30 or not new_items:
                break
            offset = page[-1].get("addedOn")
            if offset is None:
                break
            params["offset"] = offset
        return items

    def scrape(self) -> list[dict]:
        index = self._get_json({"format": "json", "view": "list"})
        categories = index.get("collection", {}).get("categories", [])
        seasons = [value for value in categories
                   if re.fullmatch(r"\d{4}-\d{4}", value)]
        if not seasons:
            raise ValueError("Calendar feed did not expose season categories")

        records = []
        seen_ids: set[str] = set()
        for season in seasons:
            for item in self._season_items(season):
                item_id = item.get("id")
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                record = _event_record(item)
                if record:
                    records.append(record)
        return records


def main():
    KatiaGocsCrawler().run()


if __name__ == "__main__":
    main()
