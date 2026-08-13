import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Sejong Soloists"
SOURCE_URL = "https://www.sejongsolo.org/"
LIST_URL = urljoin(SOURCE_URL, "project/performance")
TIMEOUT = 30
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
}

# The ensemble tours, so geography is inferred from the advertised venue rather
# than from its Seoul office. These markers cover the site's retained catalogue.
CITY_MARKERS = {
    "서울": "Seoul",
    "예술의전당": "Seoul",
    "일신홀": "Seoul",
    "플랫폼-엘": "Seoul",
    "레스케이프 호텔": "Seoul",
    "주한 리스트 헝가리 문화원": "Seoul",
    "부산": "Busan",
    "대구": "Daegu",
    "인천": "Incheon",
    "광주": "Gwangju",
    "대전": "Daejeon",
    "울산": "Ulsan",
    "세종": "Sejong",
    "수원": "Suwon",
    "성남": "Seongnam",
    "고양": "Goyang",
    "제주": "Jeju",
    "통영": "Tongyeong",
    "평창": "Pyeongchang",
    "뉴욕": "New York",
    "New York": "New York",
    "Carnegie Hall": "New York",
}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _city(venue: str) -> str | None:
    for marker, city in CITY_MARKERS.items():
        if marker.casefold() in venue.casefold():
            return city
    return None


def _time(value: str) -> str | None:
    match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", value, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).upper() == "PM" and hour != 12:
        hour += 12
    elif match.group(3).upper() == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _description(soup: BeautifulSoup) -> str | None:
    main = soup.select_one("main")
    title = main.select_one("h2") if main else None
    if not title:
        return None
    parts = []
    for node in title.find_all_next(["p", "h3"]):
        if node.find_parent("footer"):
            break
        text = _clean(node.get_text(" ", strip=True))
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts) or None


def _detail(session: requests.Session, item: dict) -> dict | None:
    response = session.get(item["url"], timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    title_node = soup.select_one("main h2")
    title = _clean(title_node.get_text(" ", strip=True) if title_node else item["title"])
    if not title:
        return None
    return {
        "title": title,
        "date": item["date"],
        "url": response.url,
        "time_from": item["time_from"],
        "venue": item["venue"],
        "city": item["city"],
        "country_code": "KR" if item["city"] != "New York" else "US",
        "description": _description(soup),
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


def _list_items(soup: BeautifulSoup) -> list[dict]:
    items = []
    for node in soup.select(".performance-list-item[data-performance-date]"):
        link = node.select_one('a[href*="/project/performance/"]')
        event_date = _clean(node.get("data-performance-date"))
        paragraphs = [_clean(part.get_text(" ", strip=True)) for part in node.select("p")]
        schedule = next((value for value in paragraphs if re.search(r"\b(?:AM|PM)\b", value, re.I)), "")
        venue = ""
        if schedule in paragraphs:
            index = paragraphs.index(schedule)
            venue = paragraphs[index + 1] if index + 1 < len(paragraphs) else ""
        title = _clean(node.select_one("img[alt]").get("alt")) if node.select_one("img[alt]") else ""
        city = _city(venue)
        try:
            event_date = date.fromisoformat(event_date).isoformat()
        except ValueError:
            continue
        if not all((link, title, venue, city)):
            log_message(
                "Skipped performance with incomplete geography or metadata",
                event="crawler_item_skipped",
                level="warning",
                url=urljoin(LIST_URL, link.get("href")) if link else LIST_URL,
                error_type="IncompleteEventData",
                error_message="Required title, venue, city, or detail URL is missing",
            )
            continue
        items.append({
            "title": title,
            "date": event_date,
            "url": urljoin(LIST_URL, link.get("href")),
            "time_from": _time(schedule),
            "venue": venue,
            "city": city,
        })
    return items


class SejongSoloOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sejongsolo_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="KR",
        upload_target="classical",
        columns=[
            "title", "date", "url", "time_from", "venue", "city",
            "country_code", "description", "source_url", "source",
        ],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(HEADERS)
        items = {}
        page = 1
        while True:
            response = session.get(LIST_URL, params={"page": page}, timeout=TIMEOUT)
            response.raise_for_status()
            page_items = _list_items(BeautifulSoup(response.text, "html.parser"))
            if not page_items:
                break
            new_urls = {item["url"] for item in page_items} - set(items)
            if not new_urls:
                break
            items.update({item["url"]: item for item in page_items})
            page += 1

        records = []
        for item in items.values():
            try:
                record = _detail(session, item)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    "Performance detail skipped",
                    event="crawler_detail_failed",
                    level="warning",
                    url=item["url"],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(records, key=lambda item: (item["date"], item["time_from"] or "", item["title"]))


def main():
    SejongSoloOrgCrawler().run()


if __name__ == "__main__":
    main()
