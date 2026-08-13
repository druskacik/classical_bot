import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Cheongju Arts Center"
SOURCE_URL = "https://www.cheongju.go.kr/ac/"
BASE_URL = SOURCE_URL.rstrip("/")
LIST_URL = f"{BASE_URL}/selectTnAhPblprfrListU3.do"
ARCHIVE_START_YEAR = 2015
FUTURE_YEARS = 2
TIMEOUT = 30
MAX_WORKERS = 8
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    # The site issues the cookies used by its request filter on the landing page.
    response = session.get(f"{BASE_URL}/index.do", timeout=TIMEOUT)
    response.raise_for_status()
    return session


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dates(value: str) -> list[date]:
    found = []
    current_year = None
    for year, month, day in re.findall(
        r"(?:(20\d{2})\s*[.년/-]\s*)?(\d{1,2})\s*[.월/-]\s*(\d{1,2})", value
    ):
        if year:
            current_year = int(year)
        if current_year:
            parsed = _date(current_year, int(month), int(day))
            if parsed:
                found.append(parsed)
    if len(found) == 2 and re.search(r"[~∼]|(?:부터|까지)", value):
        span = (found[1] - found[0]).days
        if 0 <= span <= 31:
            found = [found[0] + timedelta(days=offset) for offset in range(span + 1)]
    return list(dict.fromkeys(found))


def _times(value: str) -> list[str]:
    times = []
    for hour, minute in re.findall(r"(?<!\d)([01]?\d|2[0-3])\s*:\s*([0-5]\d)", value):
        times.append(f"{int(hour):02d}:{minute}")
    return list(dict.fromkeys(times))


def _labelled_values(soup: BeautifulSoup) -> dict[str, str]:
    values = {}
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            values[_clean(cells[0].get_text(" ", strip=True))] = _clean(
                " ".join(cell.get_text(" ", strip=True) for cell in cells[1:])
            )
    for label in soup.select("dt"):
        value = label.find_next_sibling("dd")
        if value:
            values[_clean(label.get_text(" ", strip=True))] = _clean(
                value.get_text(" ", strip=True)
            )
    return values


def _description(soup: BeautifulSoup) -> str | None:
    heading = next(
        (tag for tag in soup.find_all(["h2", "h3", "h4", "strong"]) if "공연 상세정보" in _clean(tag.get_text())),
        None,
    )
    if not heading:
        return None
    parts = []
    for sibling in heading.find_all_next():
        if sibling.name in {"h2", "h3", "h4"} and sibling is not heading:
            break
        if sibling.name in {"p", "div", "li"} and not sibling.find(["p", "div", "li"]):
            text = _clean(sibling.get_text(" ", strip=True))
            if text and text not in {"목록", "공연 상세정보"}:
                parts.append(text)
    return "\n".join(dict.fromkeys(parts)) or None


def _occurrences(schedule: str, fallback: str) -> list[tuple[str, str | None]]:
    parsed_dates = _dates(schedule) or _dates(fallback)
    parsed_times = _times(schedule)
    if not parsed_dates:
        return []
    if len(parsed_dates) == 1 and parsed_times:
        return [(parsed_dates[0].isoformat(), value) for value in parsed_times]
    if len(parsed_dates) == len(parsed_times):
        return [(day.isoformat(), event_time) for day, event_time in zip(parsed_dates, parsed_times)]
    event_time = parsed_times[0] if len(parsed_times) == 1 else None
    return [(day.isoformat(), event_time) for day in parsed_dates]


def _detail(session: requests.Session, item: dict) -> list[dict]:
    response = session.get(item["url"], timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    values = _labelled_values(soup)
    schedule = next(
        (value for key, value in values.items() if "공연시간" in re.sub(r"\s+", "", key)),
        "",
    )
    venue = next(
        (value for key, value in values.items() if "공연장" in re.sub(r"\s+", "", key)),
        "",
    ) or item["venue"]
    venue = _clean(venue)
    if not venue or "전시실" in venue:
        return []
    common = {
        "title": item["title"],
        "url": item["url"],
        "venue": venue,
        "city": "Cheongju",
        "description": _description(soup),
    }
    return [
        {**common, "date": event_date, "time_from": event_time}
        for event_date, event_time in _occurrences(schedule, item["date_text"])
    ]


def _items(session: requests.Session, year: int) -> list[dict]:
    response = session.get(
        LIST_URL,
        params={
            "key": "16202",
            "si10": "3",
            "sc9": f"{year}0101",
            "sc10": f"{year}1231",
            "sortGb": "asc",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    if response.url.endswith("/common/block.do"):
        raise requests.RequestException("request was redirected to the site's block page")
    soup = BeautifulSoup(response.text, "html.parser")
    items = []
    for row in soup.select("div.info_wrap li"):
        link = row.select_one("div.tit a[href*='pblprfrNo=']")
        date_node = row.select_one("div.date")
        if not link or not date_node:
            continue
        text = _clean(link.get_text(" ", strip=True))
        match = re.match(r"(.+?)\s*\[([^\]]+)\]\s*$", text)
        title = _clean(match.group(1) if match else text)
        venue = _clean(match.group(2) if match else "")
        if title and venue and "전시실" not in venue:
            items.append(
                {
                    "title": title,
                    "venue": venue,
                    "date_text": _clean(date_node.get_text(" ", strip=True)),
                    "url": urljoin(response.url, link.get("href")),
                }
            )
    return items


class CheongjuGoKrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="cheongju_go_kr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="KR",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def scrape(self) -> list[dict]:
        session = _session()
        items = {}
        for year in range(ARCHIVE_START_YEAR, date.today().year + FUTURE_YEARS + 1):
            for item in _items(session, year):
                items[item["url"]] = item

        records = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_detail, session, item): item for item in items.values()}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Concert detail skipped",
                        event="crawler_detail_failed",
                        level="warning",
                        url=item["url"],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return records


def main():
    CheongjuGoKrCrawler().run()


if __name__ == "__main__":
    main()
