import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Національна опера України"
SOURCE_URL = "https://www.opera.com.ua/"
CALENDAR_URL = urljoin(SOURCE_URL, "afisha")
DEFAULT_VENUE = "Національна опера України"
DEFAULT_CITY = "Київ"
ARCHIVE_YEAR = 2015

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
}


def _clean_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _month_sequence(start_year: int):
    today = date.today()
    year, month = start_year, 1
    # Programmes are normally announced only a few months ahead. Six empty
    # future months provide a safe, bounded way to follow a longer season.
    empty_future_months = 0
    while year < today.year or month <= today.month or empty_future_months < 6:
        yield year, month
        if year > today.year or (year == today.year and month > today.month):
            empty_future_months += 1
        month += 1
        if month == 13:
            year, month = year + 1, 1


def _request_soup(session: requests.Session, url: str, *, params=None) -> BeautifulSoup:
    log_message("Fetching crawler page", event="crawler_url_fetch", url=url)
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def _page_count(soup: BeautifulSoup) -> int:
    pages = [0]
    for link in soup.select(".view-afisha .pager a[href*='page=']"):
        match = re.search(r"[?&]page=(\d+)", link.get("href", ""))
        if match:
            pages.append(int(match.group(1)))
    return max(pages) + 1


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _parse_rows(soup: BeautifulSoup, year: int, month: int) -> list[dict]:
    records = []
    for row in soup.select(".view-afisha .views-row"):
        date_element = row.select_one(".item .date")
        time_element = row.select_one(".item .row_date")
        title_links = row.select(".item .right_part .title a")
        if date_element is None or time_element is None or not title_links:
            continue

        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", date_element.get_text(strip=True))
        if not match:
            continue
        day, displayed_month = map(int, match.groups())
        if displayed_month != month:
            continue
        try:
            event_date = date(year, month, day).isoformat()
        except ValueError:
            continue

        titles = [_clean_text(link.get_text(" ", strip=True)) for link in title_links]
        titles = list(dict.fromkeys(title for title in titles if title))
        href = title_links[0].get("href")
        if not titles or not href:
            continue

        timing = time_element.get_text(" ", strip=True)
        start_match = re.search(r"Початок:\s*(\d{1,2}:\d{2})", timing)
        end_match = re.search(r"Завершення:\s*(\d{1,2}:\d{2})", timing)
        if not start_match:
            continue
        records.append(
            {
                "title": " / ".join(titles),
                "date": event_date,
                "url": _base_url(urljoin(SOURCE_URL, href)),
                "time_from": start_match.group(1).zfill(5),
                "time_to": end_match.group(1).zfill(5) if end_match else None,
                "venue": DEFAULT_VENUE,
                "city": DEFAULT_CITY,
            }
        )
    return records


def _description(session: requests.Session, url: str) -> str | None:
    try:
        soup = _request_soup(session, url)
    except requests.RequestException as error:
        log_message(
            "Concert detail fetch failed",
            event="crawler_detail_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    content = soup.select_one(".view-afisha .views-row")
    if content is None:
        content = soup.select_one(".region-content")
    if content is None:
        return None
    text = _clean_text(content.get_text("\n", strip=True))
    return text or None


class OperaComUaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="opera_com_ua",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="UA",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self, archive_year: int = ARCHIVE_YEAR):
        self.archive_year = archive_year

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(HEADERS)
        candidates = []

        for year, month in _month_sequence(self.archive_year):
            params = {"month": f"01-{month:02d}-{year}"}
            first = _request_soup(session, CALENDAR_URL, params=params)
            candidates.extend(_parse_rows(first, year, month))
            for page in range(1, _page_count(first)):
                page_params = {**params, "page": page}
                soup = _request_soup(session, CALENDAR_URL, params=page_params)
                candidates.extend(_parse_rows(soup, year, month))

        unique = {}
        for record in candidates:
            key = tuple(record[field] for field in ("title", "date", "time_from", "venue", "url"))
            unique[key] = record
        records = list(unique.values())

        descriptions = {}
        urls = list(dict.fromkeys(record["url"] for record in records))
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_description, session, url): url for url in urls}
            for future in as_completed(futures):
                descriptions[futures[future]] = future.result()
        for record in records:
            record["description"] = descriptions.get(record["url"])

        log_message(
            "Calendar parsed",
            event="crawler_calendar_parsed",
            url=CALENDAR_URL,
            record_count=len(records),
        )
        return records


def main():
    OperaComUaCrawler().run()


if __name__ == "__main__":
    main()
