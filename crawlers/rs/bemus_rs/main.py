import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "BEMUS"
SOURCE_URL = "https://www.bemus.rs/sr/"
PROGRAM_URL = urljoin(SOURCE_URL, "program.html")
ARCHIVE_URL = urljoin(SOURCE_URL, "arhiva-bemus.html")

MONTHS = {
    "јануар": 1,
    "фебруар": 2,
    "март": 3,
    "април": 4,
    "мај": 5,
    "јун": 6,
    "јул": 7,
    "август": 8,
    "септембар": 9,
    "октобар": 10,
    "новембар": 11,
    "децембар": 12,
}

DATE_LINE_RE = re.compile(
    r"(?P<day>\d{1,2})\s*\.\s*(?P<month>[А-Яа-яЉЊЕеШшЂђЖжЧчЋћЏџ]+)"
    r"\s*,?\s*(?P<hour>\d{1,2})[.:](?P<minute>\d{2})"
    r"(?:\s*(?:ч\.?|часова))?\s*[,.-]?\s*(?P<venue>[^\n]+)",
    re.IGNORECASE,
)


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    value = value.replace("\xa0", " ").replace("\u202f", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n\n", value).strip(" \n,.-")
    return value or None


def element_text(element: Tag | None) -> str | None:
    if element is None:
        return None
    return clean_text(element.get_text("\n", strip=True))


def extract_year(page: BeautifulSoup, url: str) -> int | None:
    heading = element_text(page.select_one("main h1")) or ""
    match = re.search(r"\b(19|20)\d{2}\b", heading)
    if not match:
        match = re.search(r"\b(19|20)\d{2}\b", url)
    return int(match.group()) if match else None


def parse_date(day: str, month: str, year: int) -> str | None:
    month_number = MONTHS.get(month.casefold().rstrip("."))
    if month_number is None:
        return None
    try:
        return date(year, month_number, int(day)).isoformat()
    except ValueError:
        return None


def parse_leading_details(text: str, year: int) -> tuple[str | None, str | None, str | None]:
    match = DATE_LINE_RE.search(text[:350])
    if not match:
        return None, None, None
    event_date = parse_date(match.group("day"), match.group("month"), year)
    event_time = f'{int(match.group("hour")):02d}:{match.group("minute")}:00'
    venue = clean_text(match.group("venue").split("\n", 1)[0])
    return event_date, event_time, venue


def derive_title(item: Tag, description: str, linked_title: str | None) -> str | None:
    if linked_title:
        return clean_text(linked_title)
    strong_parts = []
    for strong in item.select(".introtext strong, p strong"):
        text = clean_text(strong.get_text(" ", strip=True))
        if not text:
            continue
        if re.match(r"^(диригент|солист|режиј|програм|уметнички руководилац)\b", text, re.I):
            break
        strong_parts.append(text)
        if len(strong_parts) == 3:
            break
    if strong_parts:
        return " / ".join(dict.fromkeys(strong_parts))

    lines = [line.strip(" ,.-") for line in description.splitlines() if line.strip(" ,.-")]
    return clean_text(lines[1] if len(lines) > 1 else (lines[0] if lines else None))


class BemusRsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="bemus_rs",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="RS",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "time_from", "venue", "title"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "classical-bot/1.0 (+concert catalogue crawler)"})

    def fetch(self, url: str) -> BeautifulSoup:
        log_message("Fetching BEMUS page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def season_urls(self) -> list[str]:
        archive = self.fetch(ARCHIVE_URL)
        urls = [PROGRAM_URL]
        for link in archive.select("main a[href]"):
            href = urljoin(ARCHIVE_URL, link.get("href", ""))
            if urlparse(href).netloc == urlparse(SOURCE_URL).netloc and re.search(
                r"(?:bemus|бемус)(?:-srp)?\.html$", href, re.IGNORECASE
            ):
                urls.append(href)
        return list(dict.fromkeys(urls))

    def detail_description(self, url: str, fallback: str) -> str:
        try:
            page = self.fetch(url)
        except requests.RequestException as error:
            log_message(
                "BEMUS detail page fetch failed; using programme summary",
                event="crawler_detail_fetch_failed",
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return fallback
        article = page.select_one("main .com-content-article")
        if article:
            for unwanted in article.select("nav, figure, .pager, .pagination"):
                unwanted.decompose()
        return element_text(article) or fallback

    def parse_item(self, item: Tag, year: int, page_url: str) -> dict | None:
        description = element_text(item.select_one(".introtext") or item)
        if not description:
            return None

        link = item.select_one("figure a[href], .item-image a[href]")
        event_url = urljoin(page_url, link.get("href")) if link else page_url
        linked_title = link.get("title") if link else None

        day = element_text(item.select_one(".datum"))
        month = element_text(item.select_one(".mesec"))
        time_text = element_text(item.select_one(".vreme"))
        venue = element_text(item.select_one(".sala"))
        event_date = parse_date(day, month, year) if day and month else None
        time_from = None
        if time_text and re.fullmatch(r"\d{1,2}[:.]\d{2}", time_text):
            hour, minute = re.split(r"[:.]", time_text)
            time_from = f"{int(hour):02d}:{minute}:00"

        # Imported legacy entries have publication metadata in the date boxes.
        # Their real occurrence data is the first line of the programme text.
        legacy_date, legacy_time, legacy_venue = parse_leading_details(description, year)
        if legacy_date:
            event_date, time_from, venue = legacy_date, legacy_time, legacy_venue

        title = derive_title(item, description, linked_title)
        if not all((title, event_date, event_url, venue)):
            log_message(
                "Skipping BEMUS entry without required occurrence fields",
                event="crawler_record_skipped",
                url=event_url,
                year=year,
            )
            return None

        if link:
            description = self.detail_description(event_url, description)
        return {
            "title": title,
            "date": event_date,
            "url": event_url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": "Beograd",
            "description": description,
        }

    def scrape_season(self, season_url: str) -> list[dict]:
        records = []
        page_url = season_url
        seen_pages = set()
        year = None
        while page_url not in seen_pages:
            seen_pages.add(page_url)
            page = self.fetch(page_url)
            year = year or extract_year(page, season_url)
            if year is None:
                return records
            items = page.select("main .blog-item")
            for item in items:
                record = self.parse_item(item, year, page_url)
                if record:
                    records.append(record)

            next_url = None
            for link in page.select("main .pagination a[href], main nav a[href]"):
                candidate = urljoin(page_url, link.get("href", ""))
                if "start=" in candidate and candidate not in seen_pages:
                    next_url = candidate
                    break
            if not next_url:
                break
            page_url = next_url
        return records

    def scrape(self) -> list[dict]:
        records = []
        for season_url in self.season_urls():
            try:
                records.extend(self.scrape_season(season_url))
            except requests.RequestException as error:
                log_message(
                    "BEMUS season page failed",
                    event="crawler_season_fetch_failed",
                    url=season_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message(
            "BEMUS scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    BemusRsCrawler().run()


if __name__ == "__main__":
    main()
