import html
import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "National Chorus of Korea"
SOURCE_URL = "http://nationalchorus.or.kr/"
EVENTS_URL = urljoin(SOURCE_URL, "events/")
TIMEOUT = 60
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
}

# The calendar's location property is unused. Venues are entered as prose, so
# these first-party place names provide the defensible city inference needed for
# both Seoul performances and the ensemble's tours.
PLACE_CITIES = {
    "Walt Disney": ("Los Angeles", "US"),
    "오사카": ("Osaka", "JP"),
    "도쿄": ("Tokyo", "JP"),
    "예술의전당": ("Seoul", "KR"),
    "롯데콘서트홀": ("Seoul", "KR"),
    "국립극장": ("Seoul", "KR"),
    "연세대학교": ("Seoul", "KR"),
    "대한민국역사박물관": ("Seoul", "KR"),
    "국립중앙박물관": ("Seoul", "KR"),
    "광화문": ("Seoul", "KR"),
    "강북": ("Seoul", "KR"),
    "강동": ("Seoul", "KR"),
    "금나래": ("Seoul", "KR"),
    "서울": ("Seoul", "KR"),
    "수원": ("Suwon", "KR"),
    "천안": ("Cheonan", "KR"),
    "울산": ("Ulsan", "KR"),
    "대구": ("Daegu", "KR"),
    "대전": ("Daejeon", "KR"),
    "부산": ("Busan", "KR"),
    "광주": ("Gwangju", "KR"),
    "인천": ("Incheon", "KR"),
    "남동소래": ("Incheon", "KR"),
    "부평": ("Incheon", "KR"),
    "세종": ("Sejong", "KR"),
    "제주": ("Jeju", "KR"),
    "서귀포": ("Seogwipo", "KR"),
    "성남": ("Seongnam", "KR"),
    "고양": ("Goyang", "KR"),
    "평택": ("Pyeongtaek", "KR"),
    "화성": ("Hwaseong", "KR"),
    "남양성모": ("Hwaseong", "KR"),
    "안성": ("Anseong", "KR"),
    "과천": ("Gwacheon", "KR"),
    "군포": ("Gunpo", "KR"),
    "의정부": ("Uijeongbu", "KR"),
    "김포": ("Gimpo", "KR"),
    "오산": ("Osan", "KR"),
    "광명": ("Gwangmyeong", "KR"),
    "이천": ("Icheon", "KR"),
    "계룡": ("Gyeryong", "KR"),
    "보령": ("Boryeong", "KR"),
    "군산": ("Gunsan", "KR"),
    "부안": ("Buan", "KR"),
    "고창": ("Gochang", "KR"),
    "금산": ("Geumsan", "KR"),
    "서천": ("Seocheon", "KR"),
    "서산": ("Seosan", "KR"),
    "나주": ("Naju", "KR"),
    "목포": ("Mokpo", "KR"),
    "김해": ("Gimhae", "KR"),
    "창원": ("Changwon", "KR"),
    "성산아트홀": ("Changwon", "KR"),
    "거제": ("Geoje", "KR"),
    "진주": ("Jinju", "KR"),
    "함양": ("Hamyang", "KR"),
    "의령": ("Uiryeong", "KR"),
    "창녕": ("Changnyeong", "KR"),
    "포항": ("Pohang", "KR"),
    "안동": ("Andong", "KR"),
    "의성": ("Uiseong", "KR"),
    "청송": ("Cheongsong", "KR"),
    "김천": ("Gimcheon", "KR"),
    "제천": ("Jecheon", "KR"),
    "충주": ("Chungju", "KR"),
    "속초": ("Sokcho", "KR"),
    "강릉": ("Gangneung", "KR"),
    "동해": ("Donghae", "KR"),
    "평창": ("Pyeongchang", "KR"),
    "알펜시아": ("Pyeongchang", "KR"),
    "계촌": ("Pyeongchang", "KR"),
    "정선": ("Jeongseon", "KR"),
    "태백": ("Taebaek", "KR"),
}
SEOUL_DEFAULT_MARKERS = {
    "예술의전당",
    "롯데콘서트홀",
    "국립극장",
    "연세대학교",
    "대한민국역사박물관",
    "국립중앙박물관",
    "광화문",
    "강북",
    "강동",
    "금나래",
    "서울",
}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _embedded_events(page: str) -> list[dict]:
    marker = re.search(r"stecJsonEvents\['[^']+'\]\s*=\s*", page)
    if not marker:
        raise ValueError("embedded calendar data not found")
    events, _ = json.JSONDecoder().raw_decode(page[marker.end():])
    if not isinstance(events, list):
        raise ValueError("embedded calendar data is not a list")
    return events


def _description(event: dict) -> str | None:
    soup = BeautifulSoup(event.get("description") or "", "html.parser")
    text = _clean(soup.get_text(" ", strip=True))
    return text or None


def _time(text: str) -> str | None:
    match = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*:\s*([0-5]\d)(?!\d)", text)
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _venue(text: str, title: str) -> str | None:
    match = re.search(
        r"(?:장소|공연장)\s*[:：]\s*(.+?)(?=\s+(?:일시|공연일시|주최|주관|출연|티켓|입장|문의|프로그램|관람)\s*[:：]|$)",
        text,
    )
    if match:
        venue = _clean(match.group(1))
        # Some editors put the next labelled field on the same line without a
        # colon. Avoid retaining it as part of the venue.
        venue = re.split(r"\s+공연일시\s*[:：]", venue, maxsplit=1)[0]
        if venue and venue not in {"미정", "추후공지", "추후 공지"}:
            return venue

    combined = f"{title} {text}"
    # Older entries often put only a hall name in the title/body. Select the
    # shortest phrase containing a recognizable venue suffix.
    pattern = re.compile(
        r"([가-힣A-Za-z0-9·& ]{2,40}?(?:콘서트홀|챔버홀|IBK홀|아트센터|아트홀|"
        r"예술의전당|문화예술회관|문화회관|문예회관|시민회관|대극장|극장|성당|"
        r"기념관|박물관|뮤직텐트|문화전당|다락원))"
    )
    candidates = [_clean(value) for value in pattern.findall(combined)]
    # Prefer a phrase beginning at a known place marker. This recovers compact
    # legacy titles such as "...<포항>" and avoids leading date/time fragments.
    for marker in PLACE_CITIES:
        marker_pattern = re.compile(
            rf"({re.escape(marker)}[가-힣A-Za-z0-9·& ]{{0,35}}?(?:콘서트홀|챔버홀|IBK홀|"
            r"아트센터|아트홀|예술의전당|문화예술회관|문화회관|문예회관|시민회관|"
            r"대극장|극장|성당|기념관|박물관|뮤직텐트|문화전당))"
        )
        candidates.extend(_clean(value) for value in marker_pattern.findall(combined))
    candidates = [value for value in candidates if any(key in value for key in PLACE_CITIES)]
    return min(candidates, key=len) if candidates else None


def _geography(venue: str, title: str, text: str) -> tuple[str, str] | None:
    haystack = f"{venue} {title} {text}"
    for marker, geography in PLACE_CITIES.items():
        if marker not in SEOUL_DEFAULT_MARKERS and marker in haystack:
            return geography
    for marker in SEOUL_DEFAULT_MARKERS:
        if marker in haystack:
            return PLACE_CITIES[marker]
    for marker, geography in PLACE_CITIES.items():
        if marker in haystack:
            return geography
    return None


def _record(event: dict) -> dict | None:
    title = _clean(event.get("title"))
    start = _clean(event.get("start_date"))
    try:
        event_date = datetime.strptime(start[:10], "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return None
    description = _description(event)
    evidence = description or ""
    venue = _venue(evidence, title)
    if not title or not venue:
        return None
    geography = _geography(venue, title, evidence)
    if not geography:
        return None
    city, country_code = geography
    url = _clean(event.get("permalink"))
    if not url:
        return None
    return {
        "title": title,
        "date": event_date,
        "url": urljoin(SOURCE_URL, url),
        "time_from": _time(evidence or title),
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class NationalChorusCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nationalchorus_or_kr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="KR",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(EVENTS_URL, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        events = _embedded_events(response.text)
        records = []
        for event in events:
            record = _record(event)
            if record:
                records.append(record)
        log_message(
            "Calendar parsed",
            event="crawler_calendar_parsed",
            url=EVENTS_URL,
            record_count=len(records),
            source_event_count=len(events),
        )
        return records


def main():
    NationalChorusCrawler().run()


if __name__ == "__main__":
    main()
