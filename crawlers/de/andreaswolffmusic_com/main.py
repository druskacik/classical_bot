import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Andreas Wolff"
SOURCE_URL = "https://andreaswolffmusic.com/"
LIVE_URL = urljoin(SOURCE_URL, "pages/live")
MONTHS = {
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "APRIL": 4,
    "MAY": 5,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "SEPTEMBER": 9,
    "OCTOBER": 10,
    "NOVEMBER": 11,
    "DECEMBER": 12,
}
DATE_RE = re.compile(
    r"^(?P<first>\d{1,2})(?:\s*[-–]\s*(?P<last>\d{1,2}))?\s+"
    r"(?P<month>[A-Z]+)(?:\s*\|\s*(?P<time>.+))?$"
)
TIME_RE = re.compile(r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>AM|PM)$", re.I)


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _parse_time(value: str | None) -> str | None:
    if not value:
        return None
    match = TIME_RE.fullmatch(_normalise(value))
    if not match:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("period").upper() == "PM":
        hour += 12
    return f'{hour:02d}:{int(match.group("minute") or 0):02d}'


def _dates(year: int, first: int, last: int | None, month: int):
    try:
        start = date(year, month, first)
        end = date(year, month, last or first)
    except ValueError:
        return
    if end < start:
        return
    current = start
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _event_blocks(soup: BeautifulSoup):
    for heading in soup.select("main h2"):
        year_text = _normalise(heading.get_text(" "))
        if not re.fullmatch(r"20\d{2}", year_text):
            continue
        container = heading.find_next_sibling("div")
        if container is None:
            continue
        for paragraph in container.find_all("p", recursive=False):
            yield int(year_text), paragraph


class AndreasWolffMusicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="andreaswolffmusic_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="DE",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching live calendar", event="crawler_url_fetch", url=LIVE_URL)
        response = requests.get(
            LIVE_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []

        for year, paragraph in _event_blocks(soup):
            strong = paragraph.find("strong")
            if strong is None:
                continue
            date_text = _normalise(strong.get_text(" ")).upper()
            match = DATE_RE.fullmatch(date_text)
            if not match or match.group("month") not in MONTHS:
                continue

            lines = [_normalise(text) for text in paragraph.stripped_strings]
            lines = [line for line in lines if line and line.upper() != date_text]
            if len(lines) < 2 or lines[0].upper() in {"PRIVATE EVENT", "LIVING ROOM CONCERT"}:
                continue

            # Calendar rows are consistently: date, venue/event name, city,
            # followed by ticket or information text. Named concert rows have
            # no venue and are therefore deliberately skipped.
            venue, city = lines[0], lines[1]
            if ":" in venue or city.lower().startswith(("more info", "ticket", "free admission")):
                continue
            link = paragraph.find("a", href=True)
            event_url = urljoin(LIVE_URL, link["href"]) if link else LIVE_URL
            description = "\n".join(lines)
            title = f"{SOURCE} – {venue}"

            for event_date in _dates(
                year,
                int(match.group("first")),
                int(match.group("last")) if match.group("last") else None,
                MONTHS[match.group("month")],
            ):
                records.append(
                    {
                        "title": title,
                        "date": event_date,
                        "url": event_url,
                        "time_from": _parse_time(match.group("time")),
                        "venue": venue,
                        "city": city.title(),
                        "description": description,
                    }
                )

        log_message(
            "Live calendar parsed",
            event="crawler_scrape_completed",
            url=LIVE_URL,
            record_count=len(records),
        )
        return records


def main():
    AndreasWolffMusicCrawler().run()


if __name__ == "__main__":
    main()
