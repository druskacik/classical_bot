import html
import json
import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "부산콘서트홀"
SOURCE_URL = "https://classicbusan.busan.go.kr/"
BASE_URL = SOURCE_URL.rstrip("/")
ARCHIVE_START_YEAR = 2025
TIMEOUT = 30


def _embedded_json(page: str, marker: str) -> dict:
    match = re.search(marker, page)
    if not match:
        raise ValueError("embedded data marker not found")
    start = match.end()
    while start < len(page) and page[start].isspace():
        start += 1
    value, _ = json.JSONDecoder().raw_decode(page[start:])
    return value


def _search_filter(year: int, page_index: int = 1) -> dict:
    return {
        "Open": f"{year}-01-01",
        "Close": f"{year}-12-31",
        "TypeID": 1469,
        "PageIndex": page_index,
        "PageSize": 100,
        "LanguageID": 4101,
        "ProductionTypeID": None,
        # The server currently echoes GenreID but does not apply it. Keep the
        # complete performance feed for potential-event classification.
        "GenreID": None,
        "VenueID": 0,
        "SaleStatusID": None,
        "SearchTerm": 0,
        "SearchText": "",
        "MonthlyFilterViewModel": {
            "Year": year,
            "Month": 1,
            "SelectDay": None,
            "Today": 1,
            "VenueID": 0,
        },
        "IsClosed": False,
    }


def _search(session: requests.Session, year: int) -> list[dict]:
    records = []
    page_index = 1
    while True:
        payload = _search_filter(year, page_index)
        response = session.post(
            f"{BASE_URL}/api/historyBack/create",
            data={"value": json.dumps(payload, ensure_ascii=False)},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        key_data = response.json()
        if key_data.get("Code") != 0 or not key_data.get("Tag"):
            raise ValueError(f"could not create search key for {year}, page {page_index}")

        # Tag is already URL-encoded by the first-party endpoint. Passing it via
        # requests' ``params`` would encode it a second time and silently reset
        # the search to the site's default filter.
        response = session.get(
            f"{BASE_URL}/product/performance/search?q={key_data['Tag']}",
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()["Tag"]
        records.extend(result.get("Performances") or [])
        pager = result.get("Pager") or {}
        if page_index >= pager.get("TotalPageCount", 1):
            break
        page_index += 1
    return records


def _dates(detail: dict) -> list[str]:
    descriptions = {item["Name"]: item.get("Value") or "" for item in detail.get("Descriptions", [])}
    raw = descriptions.get("공연일자", "")
    found = re.findall(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw)
    parsed = [date(int(year), int(month), int(day)) for year, month, day in found]
    if not parsed and detail.get("PlayDate"):
        parsed = [date.fromisoformat(detail["PlayDate"])]
    if len(parsed) == 2 and parsed[1] > parsed[0]:
        span = (parsed[1] - parsed[0]).days
        if span <= 31:
            parsed = [parsed[0] + timedelta(days=offset) for offset in range(span + 1)]
    return sorted({value.isoformat() for value in parsed})


def _time(detail: dict) -> str | None:
    descriptions = {item["Name"]: item.get("Value") or "" for item in detail.get("Descriptions", [])}
    match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", descriptions.get("공연시간", ""))
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def _description(detail: dict) -> str | None:
    parts = []
    if detail.get("Title"):
        parts.append(detail["Title"].strip())
    for item in detail.get("Details", []):
        soup = BeautifulSoup(html.unescape(item.get("Value") or ""), "html.parser")
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        if text:
            parts.append(text)
    return "\n".join(dict.fromkeys(parts)) or None


def _detail(session: requests.Session, item: dict) -> list[dict]:
    url = item["LinkUrl"].replace("http://", "https://")
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    detail = _embedded_json(
        response.text,
        r'var\s+body\s*=\s*new\s+Vue\s*\(\s*\{\s*el:\s*["\']#body["\']\s*,\s*data:\s*',
    )
    venue = (detail.get("Place") or item.get("VenueName") or "").strip()
    title = (detail.get("PerformanceName") or item.get("Title") or "").strip()
    if not title or not venue:
        return []
    common = {
        "title": title,
        "url": url,
        "time_from": _time(detail),
        "time_to": None,
        "venue": venue,
        "city": "Busan",
        "description": _description(detail),
    }
    return [{**common, "date": event_date} for event_date in _dates(detail)]


class ClassicBusanCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="classicbusan_busan_go_kr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="KR",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"})
        items = []
        for year in range(ARCHIVE_START_YEAR, date.today().year + 3):
            items.extend(_search(session, year))
        items = {item["LinkUrl"]: item for item in items}.values()
        records = []
        for item in items:
            try:
                records.extend(_detail(session, item))
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as error:
                log_message(
                    "Concert detail skipped",
                    event="crawler_detail_failed",
                    url=item.get("LinkUrl"),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return records


def main():
    ClassicBusanCrawler().run()


if __name__ == "__main__":
    main()
