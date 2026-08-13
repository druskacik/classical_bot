import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Bucheon City Arts Group"
SOURCE_URL = "https://www.bucheonphil.or.kr/"
CURRENT_URL = urljoin(SOURCE_URL, "front/M0000012/event/list.do")
ARCHIVE_URL = urljoin(SOURCE_URL, "front/M0000030/event/list.do")
TIMEOUT = 45
MAX_WORKERS = 12
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
}

# The organization occasionally tours. Explicit city evidence therefore takes
# precedence over the home-city venue list, and unknown locations are skipped.
CITY_MARKERS = (
    ("서울", "Seoul"),
    ("롯데콘서트홀", "Seoul"),
    ("예술의전당", "Seoul"),
    ("인천", "Incheon"),
    ("고양", "Goyang"),
    ("성남", "Seongnam"),
    ("수원", "Suwon"),
    ("대전", "Daejeon"),
    ("대구", "Daegu"),
    ("부산", "Busan"),
    ("광주", "Gwangju"),
    ("울산", "Ulsan"),
    ("제주", "Jeju"),
    ("춘천", "Chuncheon"),
    ("부천", "Bucheon"),
)
BUCHEON_VENUES = (
    "시민회관",
    "오정아트홀",
    "복사골문화센터",
    "자연생태공원",
    "한국만화박물관",
    "부천아트센터",
)
DATE_RE = re.compile(
    r"(?P<year>20\d{2})\s*(?:년|[./-])\s*"
    r"(?P<month>\d{1,2})\s*(?:월|[./-])\s*"
    r"(?P<day>\d{1,2})\s*일?"
)
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def get_soup(session: requests.Session, url: str, params=None) -> BeautifulSoup:
    response = session.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def listing_page(session: requests.Session, listing_url: str, page: int) -> tuple[list[str], int]:
    soup = get_soup(session, listing_url, params={"pageIndex": page})
    urls = []
    for anchor in soup.select('a[href*="view.do?eventId="]'):
        url = urljoin(listing_url, anchor.get("href", ""))
        if url and url not in urls:
            urls.append(url)

    page_numbers = []
    for anchor in soup.select('a[href*="pageIndex="]'):
        match = re.search(r"pageIndex=(\d+)", anchor.get("href", ""))
        if match:
            page_numbers.append(int(match.group(1)))
    return urls, max(page_numbers, default=1)


def listing_urls(session: requests.Session, listing_url: str) -> list[str]:
    first_urls, last_page = listing_page(session, listing_url, 1)
    urls = list(first_urls)
    if last_page > 1:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [
                executor.submit(listing_page, session, listing_url, page)
                for page in range(2, last_page + 1)
            ]
            for future in as_completed(futures):
                page_urls, _ = future.result()
                urls.extend(page_urls)
    return list(dict.fromkeys(urls))


def city_for_venue(venue: str) -> str | None:
    for marker, city in CITY_MARKERS:
        if marker in venue:
            return city
    if any(marker in venue for marker in BUCHEON_VENUES):
        return "Bucheon"
    return None


def occurrences(value: str) -> list[tuple[str, str | None]]:
    matches = list(DATE_RE.finditer(value))
    found = []
    for index, match in enumerate(matches):
        try:
            event_date = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        time_match = TIME_RE.search(value, match.end(), end)
        event_time = None
        if time_match:
            event_time = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
        found.append((event_date, event_time))
    return list(dict.fromkeys(found))


def detail_description(soup: BeautifulSoup) -> str | None:
    parts = []
    for selector in ("#proViewList01", "#proViewList02"):
        node = soup.select_one(selector)
        if not node:
            continue
        for unwanted in node.select("h5.blind, script, style"):
            unwanted.decompose()
        text = clean_text(node.get_text("\n", strip=True))
        if text and text not in parts:
            parts.append(text)
    return "\n\n".join(parts) or None


def parse_detail(soup: BeautifulSoup, url: str) -> list[dict]:
    title_node = soup.select_one(".txtWrap h4")
    title = clean_text(title_node.get_text(" ", strip=True) if title_node else "")
    fields = {}
    for item in soup.select(".txtList li"):
        label = item.select_one("em")
        value = item.select_one("span")
        if label and value:
            fields[clean_text(label.get_text(" ", strip=True))] = clean_text(
                value.get_text(" ", strip=True)
            )

    venue = fields.get("장소", "")
    city = city_for_venue(venue)
    dates = occurrences(fields.get("일자", ""))
    if not title or not venue or venue in {"미정", "추후공지"} or not city or not dates:
        return []

    common = {
        "title": title,
        "url": url,
        "venue": venue,
        "city": city,
        "country_code": "KR",
        "description": detail_description(soup),
    }
    return [
        {**common, "date": event_date, "time_from": event_time}
        for event_date, event_time in dates
    ]


def scrape_detail(session: requests.Session, url: str) -> list[dict]:
    return parse_detail(get_soup(session, url), url)


class BucheonphilOrKrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="bucheonphil_or_kr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="KR",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = []
        for listing_url in (CURRENT_URL, ARCHIVE_URL):
            urls.extend(listing_urls(session, listing_url))
        urls = list(dict.fromkeys(urls))

        records = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(scrape_detail, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        "Failed to scrape concert detail",
                        event="crawler_item_failed",
                        level="warning",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record["date"], record["time_from"] or "", record["title"], record["url"]
            ),
        )


def main():
    BucheonphilOrKrCrawler().run()


if __name__ == "__main__":
    main()
