import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Korean National Symphony Orchestra"
SOURCE_URL = "https://www.knso.or.kr/"
LIST_URL = f"{SOURCE_URL}front/M0000028/performance/list.do"
DETAIL_URL = f"{SOURCE_URL}front/M0000028/performance/detail.do"
ARCHIVE_START_YEAR = 2022
FUTURE_YEARS = 2
TIMEOUT = 30
MAX_WORKERS = 10
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
}

# The calendar includes touring performances. Resolve the location from the
# advertised venue instead of applying the orchestra's Seoul home by default.
CITY_MARKERS = {
    "서울": "Seoul",
    "세종예술": "Sejong",
    "예술의전당": "Seoul",
    "롯데콘서트홀": "Seoul",
    "국립극장": "Seoul",
    "국립중앙박물관": "Seoul",
    "세종문화회관": "Seoul",
    "경기아트센터": "Suwon",
    "수원": "Suwon",
    "성남": "Seongnam",
    "고양": "Goyang",
    "아람누리": "Goyang",
    "하남": "Hanam",
    "부천": "Bucheon",
    "인천": "Incheon",
    "의정부": "Uijeongbu",
    "용인": "Yongin",
    "군포": "Gunpo",
    "안양": "Anyang",
    "과천": "Gwacheon",
    "광주": "Gwangju",
    "대전": "Daejeon",
    "대구": "Daegu",
    "부산": "Busan",
    "울산": "Ulsan",
    "강릉": "Gangneung",
    "춘천": "Chuncheon",
    "원주": "Wonju",
    "청주": "Cheongju",
    "충주": "Chungju",
    "천안": "Cheonan",
    "아산": "Asan",
    "전주": "Jeonju",
    "익산": "Iksan",
    "목포": "Mokpo",
    "여수": "Yeosu",
    "순천": "Suncheon",
    "창원": "Changwon",
    "통영": "Tongyeong",
    "김해": "Gimhae",
    "포항": "Pohang",
    "경주": "Gyeongju",
    "구미": "Gumi",
    "제주": "Jeju",
}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _list_month(year: int, month: int) -> list[str]:
    response = _session().post(
        LIST_URL,
        data={
            "pageIndex": 1,
            "curYear": year,
            "curMonth": month,
            "selectDate": "",
            "type": "date",
            "performSn": "",
            "ctgry": "",
            "searchKeyword": "",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return list(dict.fromkeys(re.findall(r"fn_detail\(['\"]([^'\"]+)", response.text)))


def _field(soup: BeautifulSoup, label: str) -> str:
    for row in soup.select(".infoArea dl"):
        heading = row.select_one("dt")
        if heading and _clean(heading.get_text(" ", strip=True)) == label:
            value = row.select_one("dd")
            return _clean(value.get_text(" ", strip=True) if value else "")
    return ""


def _city(venue: str) -> str | None:
    for marker, city in CITY_MARKERS.items():
        if marker in venue:
            return city
    return None


def _occurrences(value: str) -> list[tuple[str, str | None]]:
    matches = re.findall(
        r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})(?:일)?"
        r"(?:\s*(?:\([^)]*\))?\s*([01]?\d|2[0-3]):([0-5]\d))?",
        value,
    )
    found = []
    for year, month, day, hour, minute in matches:
        try:
            event_date = date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
        event_time = f"{int(hour):02d}:{minute}" if hour else None
        found.append((event_date, event_time))
    return list(dict.fromkeys(found))


def _detail(perform_sn: str) -> list[dict]:
    url = f"{DETAIL_URL}?{urlencode({'performSn': perform_sn})}"
    response = _session().get(url, timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    title_node = soup.select_one(".scheduleInfo .infoArea h4")
    title = _clean(title_node.get_text(" ", strip=True) if title_node else "")
    venue = _field(soup, "장소")
    city = _city(venue)
    if not title or not venue or not city:
        log_message(
            "Skipping concert with unresolved required fields",
            event="crawler_record_skipped",
            url=url,
            has_title=bool(title),
            venue=venue or None,
            has_city=bool(city),
        )
        return []

    detail = soup.select_one("#scheduleDetail01")
    if detail:
        for unwanted in detail.select("h4, script, style"):
            unwanted.decompose()
    description = _clean(detail.get_text("\n", strip=True) if detail else "") or None
    common = {
        "title": title,
        "url": url,
        "venue": venue,
        "city": city,
        "description": description,
    }
    return [
        {**common, "date": event_date, "time_from": event_time}
        for event_date, event_time in _occurrences(_field(soup, "일자 / 시간"))
    ]


class KnsoOrKrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="knso_or_kr",
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
            for year in range(ARCHIVE_START_YEAR, date.today().year + FUTURE_YEARS + 1)
            for month in range(1, 13)
        ]
        ids: set[str] = set()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_list_month, *month): month for month in months}
            for future in as_completed(futures):
                year, month = futures[future]
                try:
                    ids.update(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Concert calendar request failed",
                        event="crawler_url_failed",
                        url=LIST_URL,
                        year=year,
                        month=month,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_detail, perform_sn): perform_sn for perform_sn in ids}
            for future in as_completed(futures):
                perform_sn = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Concert detail request failed",
                        event="crawler_url_failed",
                        url=f"{DETAIL_URL}?performSn={perform_sn}",
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return records


def main():
    return KnsoOrKrCrawler().run()


if __name__ == "__main__":
    main()
