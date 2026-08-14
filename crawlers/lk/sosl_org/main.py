import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.sosl.org/"
SOURCE = "Symphony Orchestra of Sri Lanka"
ARCHIVE_URLS = (
    urljoin(SOURCE_URL, "upcoming-concerts"),
    urljoin(SOURCE_URL, "past-concerts"),
)

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        1,
    )
}
MONTH_PATTERN = "|".join(
    [*MONTHS, "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"]
)
VENUES = (
    (r"lionel\s+wendt\s+theatre", "Lionel Wendt Theatre"),
    (r"bishop[’']?s\s+college\s+auditorium", "Bishop's College Auditorium"),
    (r"ladies[’']?\s+college\s+auditorium", "Ladies' College Auditorium"),
    (r"ladies[’']?\s+college\s+hall", "Ladies' College Hall"),
    (r"ladies\s+college(?:\s*,\s*colombo\s*7)?", "Ladies' College"),
    (r"\bBMICH\b", "BMICH"),
)


def _month_number(value: str) -> int:
    value = value.lower()
    if value in MONTHS:
        return MONTHS[value]
    abbreviations = {name[:3]: number for name, number in MONTHS.items()}
    return abbreviations[value[:3]]


def _days(value: str) -> list[int]:
    return [int(day) for day in re.findall(r"(?<!\d)\d{1,2}(?!\d)", value)]


def parse_dates(detail: str, title: str) -> list[str]:
    clean = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", detail, flags=re.IGNORECASE)
    year_match = re.search(r"\b(20\d{2}|19\d{2})\b", clean) or re.search(
        r"\b(20\d{2}|19\d{2})\b", title
    )
    if not year_match:
        return []
    year = int(year_match.group(1))

    month_first = re.search(
        rf"\b(?P<month>{MONTH_PATTERN})\.?\s+(?P<days>(?<!\d)\d{{1,2}}(?!\d)(?:\s*(?:&|and)\s*(?<!\d)\d{{1,2}}(?!\d))?)",
        clean,
        flags=re.IGNORECASE,
    )
    day_first = re.search(
        rf"\b(?P<days>\d{{1,2}}(?:\s*(?:&|and)\s*\d{{1,2}})?)\s+(?P<month>{MONTH_PATTERN})\b",
        clean,
        flags=re.IGNORECASE,
    )
    match = month_first or day_first
    if not match:
        return []

    dates = []
    for day in _days(match.group("days")):
        try:
            dates.append(datetime(year, _month_number(match.group("month")), day).date().isoformat())
        except ValueError:
            return []
    return dates


def parse_time(detail: str) -> str | None:
    match = re.search(r"\b(\d{1,2})(?:[.:](\d{2}))?\s*([ap])\.?m\.?\b", detail, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "p":
        hour += 12
    return f"{hour:02d}:{int(match.group(2) or 0):02d}"


def parse_venue(detail: str) -> str | None:
    for pattern, venue in VENUES:
        if re.search(pattern, detail, re.IGNORECASE):
            return venue
    return None


def _description(soup: BeautifulSoup) -> str | None:
    sections = []
    body = soup.select_one(".article-content")
    if body:
        sections.append(body.get_text("\n", strip=True))
    programme = soup.select_one(".concertProgramme .field-value")
    if programme:
        programme_text = programme.get_text("\n", strip=True)
        if programme_text and programme_text not in sections:
            sections.append(f"Programme:\n{programme_text}")
    text = "\n\n".join(sections)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def parse_event(content: str, url: str) -> list[dict]:
    soup = BeautifulSoup(content, "html.parser")
    title_element = soup.select_one("article .article-title")
    detail_element = soup.select_one("article .mainDetail .field-value")
    if not title_element or not detail_element:
        return []

    title = title_element.get_text(" ", strip=True)
    detail = detail_element.get_text(" ", strip=True)
    venue = parse_venue(detail)
    dates = parse_dates(detail, title)
    if not title or not venue or not dates:
        return []

    description = _description(soup)
    time_from = parse_time(detail)
    return [
        {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": "Colombo",
            "description": description,
        }
        for event_date in dates
    ]


class SoslCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sosl_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="LK",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers["User-Agent"] = "classical-bot/1.0 (+https://classical-scene.com/)"
        event_urls = []
        for archive_url in ARCHIVE_URLS:
            log_message("Fetching concert archive", event="crawler_url_fetch", url=archive_url)
            response = session.get(archive_url, timeout=60)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            event_urls.extend(
                urljoin(archive_url, link["href"])
                for link in soup.select(".blog .article-title a[href]")
            )

        records = []
        for url in dict.fromkeys(event_urls):
            try:
                log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
                response = session.get(url, timeout=60)
                response.raise_for_status()
                parsed = parse_event(response.text, url)
                if not parsed:
                    log_message(
                        "Skipping concert with incomplete date or venue",
                        event="crawler_event_skipped",
                        level="warning",
                        url=url,
                    )
                records.extend(parsed)
            except requests.RequestException as error:
                log_message(
                    "Concert detail request failed",
                    event="crawler_item_failed",
                    level="warning",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return records


def main():
    SoslCrawler().run()


if __name__ == "__main__":
    main()
