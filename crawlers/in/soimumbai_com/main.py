import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.soimumbai.com/"
SOURCE = "Symphony Orchestra of India"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar")
ARCHIVES_URL = urljoin(SOURCE_URL, "archives")
REQUEST_TIMEOUT = 30


def _clean_text(element) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def _fetch_soup(url: str) -> BeautifulSoup:
    log_message("Fetching SOI page", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def _parse_occurrence(value: str) -> tuple[str, str | None]:
    value = re.sub(r"\s+", " ", value).strip()
    parsed = datetime.strptime(value, "%A, %B %d, %Y at %I:%M%p")
    return parsed.date().isoformat(), parsed.strftime("%H:%M")


def _current_event_urls(calendar_soup: BeautifulSoup) -> list[str]:
    urls = {
        urljoin(SOURCE_URL, anchor["href"])
        for anchor in calendar_soup.select('a[href*="/calendar/event/"]')
    }
    return sorted(urls)


def _parse_current_event(url: str) -> dict | None:
    soup = _fetch_soup(url)
    container = soup.select_one(".conductor-detail-info")
    if container is None:
        return None

    title = _clean_text(container.select_one("h3.custom-Line-height-1"))
    headings = container.select(".upper_desc_content h4")
    if not title or len(headings) < 2:
        return None

    try:
        event_date, time_from = _parse_occurrence(_clean_text(headings[0]))
    except ValueError:
        return None

    venue = _clean_text(headings[1])
    if not venue:
        return None

    description_parts = [
        _clean_text(container.select_one(".desc1")),
        _clean_text(container.select_one(".desc")),
    ]
    description = "\n\n".join(part for part in description_parts if part) or None

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": time_from,
        "time_to": None,
        "venue": venue,
        "city": "Mumbai",
        "description": description,
    }


def _archive_dates(header: str) -> list[str]:
    header = re.sub(r"\s+", " ", header.replace("\xa0", " ")).strip()
    full_dates = re.findall(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
        r"[A-Za-z]+ \d{1,2}, \d{4}",
        header,
    )
    if full_dates:
        return [datetime.strptime(value, "%A, %B %d, %Y").date().isoformat() for value in full_dates]

    match = re.match(r"([A-Za-z]+) ([\d, &]+), (\d{4})", header)
    if not match:
        return []
    month, days, year = match.groups()
    return [
        datetime.strptime(f"{month} {day}, {year}", "%B %d, %Y").date().isoformat()
        for day in re.findall(r"\d+", days)
    ]


def _archive_records(soup: BeautifulSoup) -> list[dict]:
    records = []
    for block in soup.select(".archives_prop .set_p_properties"):
        lines = [line.strip() for line in block.get_text("\n", strip=True).splitlines() if line.strip()]
        if not lines:
            continue
        dates = _archive_dates(lines[0])
        if not dates:
            continue

        header_end = next(
            (index for index, line in enumerate(lines) if "Theatre" in line and index > 0),
            0,
        )
        header = " | ".join(lines[: header_end + 1])
        normalized_header = re.sub(r"\s+[|I]\s+", " | ", header)
        parts = [part.strip() for part in normalized_header.split("|") if part.strip()]
        venue = next((part for part in parts if "Theatre" in part), "")
        event_type = next(
            (part for part in parts if re.search(r"Concert|Recital", part, re.IGNORECASE)),
            "SOI Concert",
        )
        if not venue:
            continue

        body_lines = lines[header_end + 1 :]
        description = "\n".join(body_lines).strip() or None
        repertoire = next((line for line in body_lines if ":" in line), None)
        title = f"{event_type}: {repertoire}" if repertoire else event_type

        for event_date in dates:
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": ARCHIVES_URL,
                    "time_from": None,
                    "time_to": None,
                    "venue": venue,
                    "city": "Mumbai",
                    "description": description,
                }
            )
    return records


class SoiMumbaiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="soimumbai_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="IN",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue"],
    )

    def scrape(self) -> list[dict]:
        calendar_soup = _fetch_soup(CALENDAR_URL)
        records = []
        for url in _current_event_urls(calendar_soup):
            try:
                record = _parse_current_event(url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch SOI event detail",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records.extend(_archive_records(_fetch_soup(ARCHIVES_URL)))
        log_message(
            "SOI scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    SoiMumbaiCrawler().run()


if __name__ == "__main__":
    main()
