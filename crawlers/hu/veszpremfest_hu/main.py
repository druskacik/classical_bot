import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://veszpremfest.hu/"
SOURCE = "VeszprémFest"
ARCHIVE_URL = urljoin(SOURCE_URL, "a-fesztivalrol/archivum")
REQUEST_TIMEOUT = 30
MAX_WORKERS = 8

MONTHS = {
    "január": 1,
    "február": 2,
    "március": 3,
    "április": 4,
    "május": 5,
    "június": 6,
    "július": 7,
    "augusztus": 8,
    "szeptember": 9,
    "október": 10,
    "november": 11,
    "december": 12,
}


def _get_soup(url: str) -> BeautifulSoup:
    log_message("Fetching page", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _program_urls() -> list[str]:
    pages = [(SOURCE_URL, _get_soup(SOURCE_URL)), (ARCHIVE_URL, _get_soup(ARCHIVE_URL))]

    archive_year_urls = {
        urljoin(ARCHIVE_URL, anchor["href"])
        for anchor in pages[1][1].select('.page-archive a[href*="/a-fesztivalrol/archivum/"]')
    }
    for archive_url in sorted(archive_year_urls):
        pages.append((archive_url, _get_soup(archive_url)))

    urls = set()
    for page_url, soup in pages:
        for anchor in soup.select('a[href^="/program/"]'):
            urls.add(urljoin(page_url, anchor["href"]))
    return sorted(urls)


def _parse_datetime(value: str) -> tuple[str, str | None]:
    normalized = " ".join(value.split()).lower()
    match = re.search(
        r"(?P<year>\d{4})\.\s*(?P<month>[a-záéíóöőúüű]+)\s+"
        r"(?P<day>\d{1,2})\.(?:\s*\|)?(?:\s*(?P<time>\d{1,2}:\d{2}))?",
        normalized,
    )
    if not match:
        raise ValueError(f"Unrecognized event date: {value!r}")
    month = MONTHS.get(match.group("month"))
    if month is None:
        raise ValueError(f"Unrecognized Hungarian month: {match.group('month')!r}")
    event_date = date(int(match.group("year")), month, int(match.group("day")))
    event_time = match.group("time")
    if event_time:
        hours, minutes = event_time.split(":")
        event_time = f"{int(hours):02d}:{minutes}"
    return event_date.isoformat(), event_time


def _parse_program(url: str) -> dict | None:
    try:
        soup = _get_soup(url)
        page = soup.select_one(".page-program")
        if page is None:
            raise ValueError("Program detail container is missing")

        title_node = page.select_one("h1")
        date_node = page.select_one(".info li.date .line1")
        venue_node = page.select_one(".info li.address .line1")
        if not title_node or not date_node or not venue_node:
            raise ValueError("Required title, date, or venue is missing")

        title = title_node.get_text(" ", strip=True)
        venue = venue_node.get_text(" ", strip=True)
        if not title or not venue:
            raise ValueError("Required title or venue is empty")
        event_date, time_from = _parse_datetime(date_node.get_text(" ", strip=True))

        description_node = page.select_one(".formatted-text")
        description = None
        if description_node:
            description = description_node.get_text("\n", strip=True) or None

        return {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "venue": venue,
            "city": "Veszprém",
            "country_code": "HU",
            "description": description,
        }
    except (requests.RequestException, ValueError) as error:
        log_message(
            "Skipping invalid program",
            event="crawler_record_skipped",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


class VeszpremFestCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="veszpremfest_hu",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="HU",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def scrape(self) -> list[dict]:
        urls = _program_urls()
        records = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_parse_program, url): url for url in urls}
            for future in as_completed(futures):
                record = future.result()
                if record is not None:
                    records.append(record)
        records.sort(key=lambda record: (record["date"], record["time_from"] or "", record["title"]))
        return records


def main():
    VeszpremFestCrawler().run()


if __name__ == "__main__":
    main()
