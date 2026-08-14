import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Seoul Philharmonic Orchestra"
SOURCE_URL = "https://www.seoulphil.or.kr/"
LIST_URL = f"{SOURCE_URL}perf/selectPerfList"
DETAIL_URL = f"{SOURCE_URL}perf/view"
ARCHIVE_START_YEAR = 2016
TIMEOUT = 30

# The calendar includes overseas tours.  Korean performances are overwhelmingly
# in Seoul, but that default is used only for known Seoul venues.
VENUE_LOCATIONS = {
    "세종문화회관": ("Seoul", "KR"),
    "롯데콘서트홀": ("Seoul", "KR"),
    "예술의전당": ("Seoul", "KR"),
    "서울어린이대공원": ("Seoul", "KR"),
    "서울주교좌성당": ("Seoul", "KR"),
    "국립극장": ("Seoul", "KR"),
    "산세바스티안": ("San Sebastian", "ES"),
    "Kursaal": ("San Sebastian", "ES"),
    "쿠르잘": ("San Sebastian", "ES"),
    "메라노": ("Merano", "IT"),
    "Kurhaus": ("Merano", "IT"),
    "콘세르트헤바우": ("Amsterdam", "NL"),
    "Concertgebouw": ("Amsterdam", "NL"),
}


def _month_values():
    today = date.today()
    for year in range(ARCHIVE_START_YEAR, today.year + 1):
        last_month = today.month if year == today.year else 12
        for month in range(1, last_month + 1):
            yield f"{year}{month:02d}"


def _post_list(session, search_date=""):
    response = session.post(
        LIST_URL,
        data={
            "pageIndex": 9999,
            "searchKeyword": "",
            "searchDate": search_date,
            "perfCate1": "",
            "tabType": "",
        },
        headers={
            "Ajax": "true",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{SOURCE_URL}perf/list",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("perfList", [])


def _clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def _location_for(venue):
    for marker, location in VENUE_LOCATIONS.items():
        if marker.casefold() in venue.casefold():
            return location
    return None


def _fetch_detail(item):
    perf_no = str(item["perfNo"])
    url = f"{DETAIL_URL}?perfNo={perf_no}"
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    summary = soup.select_one(".concert_summary")
    venue = _clean_text(item.get("placeNm") or item.get("placeNmS"))
    if not venue and summary:
        label = summary.find(string=lambda text: text and text.strip() in {"장소", "Venue"})
        if label:
            container = label.parent
            sibling = container.find_next_sibling()
            venue = _clean_text(sibling.get_text(" ", strip=True) if sibling else None)
    if not venue:
        return None

    location = _location_for(venue)
    if not location:
        log_message(
            "Skipping concert with unresolved venue location",
            event="crawler_record_skipped",
            url=url,
            venue=venue,
        )
        return None
    city, country_code = location

    stamp = str(item.get("beginDate") or "")
    if not re.fullmatch(r"\d{12}", stamp):
        return None
    title = _clean_text(html.unescape(item.get("perfName") or ""))
    if not title:
        return None

    description = _clean_text(summary.get_text("\n", strip=True) if summary else None)
    finish = str(item.get("finishDate") or "")
    return {
        "title": title,
        "date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}",
        "url": url,
        "time_from": f"{stamp[8:10]}:{stamp[10:12]}",
        "time_to": f"{finish[8:10]}:{finish[10:12]}" if re.fullmatch(r"\d{12}", finish) else None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class SeoulPhilCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="seoulphil_or_kr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="KR",
        upload_target="classical",
        dedupe_subset=["url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        items = {}

        # The undated query is the site's complete future feed.  Monthly dated
        # queries expose the still-published archive.
        for item in _post_list(session):
            items[str(item["perfNo"])] = item
        for search_date in _month_values():
            for item in _post_list(session, search_date):
                items[str(item["perfNo"])] = item

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_fetch_detail, item) for item in items.values()]
            for future in as_completed(futures):
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        "Concert detail request failed",
                        event="crawler_url_fetch_failed",
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records.sort(key=lambda row: (row["date"], row["time_from"] or "", row["url"]))
        return records


def main():
    SeoulPhilCrawler().run()


if __name__ == "__main__":
    main()
