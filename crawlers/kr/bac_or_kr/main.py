import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Bucheon Arts Center"
SOURCE_URL = "https://www.bac.or.kr/"
BASE_URL = SOURCE_URL.rstrip("/")
ARCHIVE_START_YEAR = 2023
FUTURE_YEARS = 10
TIMEOUT = 30
MAX_WORKERS = 8
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _embedded_json(page: str, marker: str) -> dict:
    match = re.search(marker, page)
    if not match:
        raise ValueError("embedded event data was not found")
    start = match.end()
    while start < len(page) and page[start].isspace():
        start += 1
    value, _ = json.JSONDecoder().raw_decode(page[start:])
    return value


def _search_filter(year: int, page_index: int) -> dict:
    # GenreID and TypeID are accepted but currently ignored by the endpoint.
    # Fetch the complete calendar and retain first-party performance records as
    # candidates for the potential-event classifier.
    return {
        "Open": f"{year}-01-01",
        "Close": f"{year}-12-31",
        "TypeID": None,
        "PageIndex": page_index,
        "PageSize": 100,
        "LanguageID": 4101,
        "ProductionTypeID": None,
        "GenreID": None,
        "VenueID": None,
        "SaleStatusID": None,
        "SearchTerm": 0,
        "SearchText": "",
        "MonthlyFilterViewModel": {
            "Year": year,
            "Month": 1,
            "SelectDay": None,
        },
        "IsEtcVenue": False,
        "ExcludedVenueID1": 2265,
        "ExcludedVenueID2": 2266,
        "ExcludedVenueID3": 2267,
    }


def _search_year(session: requests.Session, year: int) -> list[dict]:
    items = []
    page_index = 1
    while True:
        response = session.post(
            f"{BASE_URL}/api/historyBack/create",
            data={"value": json.dumps(_search_filter(year, page_index))},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        key = response.json().get("Tag")
        if not key:
            raise ValueError(f"search key missing for {year}, page {page_index}")

        # The first endpoint returns an already URL-encoded key.
        response = session.get(
            f"{BASE_URL}/product/performance/search?q={key}", timeout=TIMEOUT
        )
        response.raise_for_status()
        result = response.json()["Tag"]
        items.extend(result.get("Performances") or [])
        pager = result.get("Pager") or {}
        if page_index >= pager.get("TotalPageCount", 1):
            break
        page_index += 1
    return items


def _valid_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _expand(start: date, end: date) -> list[date]:
    span = (end - start).days
    if span < 0 or span > 31:
        return []
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def _occurrences(detail: dict) -> list[tuple[str, str | None]]:
    descriptions = {
        item.get("Name", ""): item.get("Value") or ""
        for item in detail.get("Descriptions") or []
    }
    schedule = descriptions.get("공연시간", "")
    play_date = detail.get("PlayDate") or ""
    year_match = re.search(r"(20\d{2})", play_date)
    default_year = int(year_match.group(1)) if year_match else None
    found = []

    # Detailed schedules use one line per performance and often omit the year.
    for line in schedule.splitlines():
        time_match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", line)
        event_time = (
            f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else None
        )
        dates = []
        for year, month, day in re.findall(
            r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", line
        ):
            parsed = _valid_date(int(year), int(month), int(day))
            if parsed:
                dates.append(parsed)
        if not dates and default_year:
            for month, day in re.findall(r"(?<!\d)(\d{1,2})[./월]\s*(\d{1,2})(?:일)?", line):
                parsed = _valid_date(default_year, int(month), int(day))
                if parsed:
                    dates.append(parsed)
        if len(dates) == 2 and "~" in line:
            dates = _expand(dates[0], dates[1])
        found.extend((value.isoformat(), event_time) for value in dates)

    if not found:
        dates = []
        for year, month, day in re.findall(
            r"(20\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", play_date
        ):
            parsed = _valid_date(int(year), int(month), int(day))
            if parsed:
                dates.append(parsed)
        if len(dates) == 2 and "~" in play_date:
            dates = _expand(dates[0], dates[1])
        time_match = re.search(
            r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", schedule or detail.get("PlayTime") or ""
        )
        event_time = (
            f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else None
        )
        found = [(value.isoformat(), event_time) for value in dates]

    return list(dict.fromkeys(found))


def _description(detail: dict) -> str | None:
    parts = []
    excluded = {"공연시간", "공연장소", "티켓", "공연문의", "예매문의"}
    for item in detail.get("Descriptions") or []:
        name = _clean(item.get("Name"))
        value = _clean(item.get("Value"))
        if name and value and name not in excluded:
            parts.append(f"{name}: {value}")
    for item in detail.get("Details") or []:
        soup = BeautifulSoup(html.unescape(item.get("Value") or ""), "html.parser")
        text = _clean(soup.get_text(" ", strip=True))
        if text:
            parts.append(text)
    return "\n".join(dict.fromkeys(parts)) or None


def _detail(session: requests.Session, item: dict) -> list[dict]:
    url = (item.get("LinkUrl") or "").replace("http://", "https://")
    if not url:
        return []
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    detail = _embedded_json(
        response.text,
        r"var\s+body\s*=\s*new\s+Vue\s*\(\s*\{\s*el:\s*[\"']#body[\"']\s*,\s*data:\s*",
    )
    title = _clean(detail.get("PerformanceName") or item.get("Title"))
    descriptions = {
        row.get("Name", ""): _clean(row.get("Value"))
        for row in detail.get("Descriptions") or []
    }
    venue = _clean(detail.get("Place") or descriptions.get("공연장소") or item.get("VenueName"))
    if venue == "기타" and descriptions.get("공연장소"):
        venue = descriptions["공연장소"]
    if not title or not venue:
        return []

    common = {
        "title": title,
        "url": url,
        "venue": venue,
        "city": "Bucheon",
        "description": _description(detail),
    }
    return [
        {**common, "date": event_date, "time_from": event_time}
        for event_date, event_time in _occurrences(detail)
    ]


class BacOrKrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="bac_or_kr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="KR",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(HEADERS)
        items = []
        # Match the public calendar's ten-year forward search horizon.
        for year in range(ARCHIVE_START_YEAR, date.today().year + FUTURE_YEARS + 1):
            items.extend(_search_year(session, year))

        # The calendar mixes performances, exhibitions, education, and events.
        # Type 1469 / 공연 is the broad performance candidate feed; its genre
        # labels still include nonclassical and ambiguous records.
        candidates = {
            item["LinkUrl"]: item
            for item in items
            if item.get("TypeID") == 1469 and item.get("LinkUrl")
        }
        records = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(_detail, session, item): item for item in candidates.values()
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as error:
                    log_message(
                        "Concert detail skipped",
                        event="crawler_detail_failed",
                        level="warning",
                        url=item.get("LinkUrl"),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(
            records,
            key=lambda row: (row["date"], row["time_from"] or "", row["title"], row["url"]),
        )


def main():
    BacOrKrCrawler().run()


if __name__ == "__main__":
    main()
