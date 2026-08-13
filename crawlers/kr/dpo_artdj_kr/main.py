import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Daejeon Philharmonic Orchestra"
SOURCE_URL = "https://dpo.artdj.kr/dpo/"
AJAX_URL = "https://dpo.artdj.kr/zlib/inc/common/ajax.php"
ARCHIVE_START_YEAR = 2018
TIMEOUT = 30
MAX_WORKERS = 6
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
}

CITY_MARKERS = {
    "서울": "Seoul",
    "부산": "Busan",
    "대구": "Daegu",
    "인천": "Incheon",
    "광주": "Gwangju",
    "울산": "Ulsan",
    "세종": "Sejong",
    "제주": "Jeju",
    "수원": "Suwon",
    "청주": "Cheongju",
    "천안": "Cheonan",
    "공주": "Gongju",
    "논산": "Nonsan",
    "전주": "Jeonju",
    "대전": "Daejeon",
}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _calendar_ids(year: int, month: int) -> set[str]:
    response = requests.post(
        AJAX_URL,
        data={"mo": "calendar", "site": "dpo", "ye": year, "m": month},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return set(re.findall(r"cl_box\('(\d+)'", response.text))


def _detail_url(event_id: str) -> str:
    query = urlencode(
        {"a_idx": "01_01_01", "bo": "perform", "d_no": event_id, "p": 1, "mo": "v"}
    )
    return f"{SOURCE_URL}?{query}"


def _parse_date(value: str) -> str | None:
    match = re.search(r"(?<!\d)(\d{2,4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)", value)
    if not match:
        return None
    year, month, day = map(int, match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_time(value: str) -> str | None:
    match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", value)
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _city(title: str, venue: str) -> str:
    evidence = f"{venue} {title}"
    for marker, city in CITY_MARKERS.items():
        if marker in evidence:
            return city
    # This is a city orchestra calendar and its unqualified venues are in its
    # home city. Explicit touring cities above override this default.
    return "Daejeon"


def _detail(event_id: str) -> dict | None:
    url = _detail_url(event_id)
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "euc-kr"
    soup = BeautifulSoup(response.text, "html.parser")

    title_node = soup.select_one("h3.concert_txt")
    title = _clean(title_node.get_text(" ", strip=True) if title_node else None)
    fields = {}
    for row in soup.select("table.detail_info tr"):
        heading = row.find("th")
        value = row.find("td")
        if heading and value:
            fields[_clean(heading.get_text(" ", strip=True)).replace("·", "").strip()] = _clean(
                value.get_text(" ", strip=True)
            )

    venue = fields.get("장 소", "")
    event_date = _parse_date(fields.get("날 짜", ""))
    if not title or not venue or not event_date:
        return None

    description_parts = []
    for node in soup.select("#tab_content_1, .tab_content_1, .perform_content"):
        text = _clean(node.get_text(" ", strip=True))
        if text:
            description_parts.append(text)

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": _parse_time(fields.get("시 간", "")),
        "time_to": None,
        "venue": venue,
        "city": _city(title, venue),
        "description": "\n".join(dict.fromkeys(description_parts)) or None,
    }


class DaejeonPhilharmonicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="dpo_artdj_kr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="KR",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def scrape(self) -> list[dict]:
        months = [
            (year, month)
            for year in range(ARCHIVE_START_YEAR, datetime.now().year + 3)
            for month in range(1, 13)
        ]
        event_ids = set()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_calendar_ids, *month): month for month in months}
            for future in as_completed(futures):
                year, month = futures[future]
                try:
                    event_ids.update(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Calendar month skipped",
                        event="crawler_calendar_failed",
                        url=AJAX_URL,
                        year=year,
                        month=month,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_detail, event_id): event_id for event_id in event_ids}
            for future in as_completed(futures):
                event_id = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        "Concert detail skipped",
                        event="crawler_detail_failed",
                        url=_detail_url(event_id),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return records


def main():
    DaejeonPhilharmonicCrawler().run()


if __name__ == "__main__":
    main()
