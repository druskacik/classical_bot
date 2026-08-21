from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "DimashNews"
SOURCE_URL = "https://en.dimashnews.com/events-schedule/"
API_URL = "https://en.dimashnews.com/wp-json/wp/v2/posts/7208"
TOUR_TITLE = "Dimash Qudaibergen – New World Tour"

COUNTRY_CODES = {
    "chile": "CL",
    "colombia": "CO",
    "germany": "DE",
    "mexico": "MX",
    "peru": "PE",
    "poland": "PL",
    "spain": "ES",
}


def _parse_location(value: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        return None

    first_code = COUNTRY_CODES.get(parts[0].casefold())
    second_code = COUNTRY_CODES.get(parts[1].casefold())
    if first_code:
        return parts[1], first_code
    if second_code:
        return parts[0].title(), second_code
    return None


def _event_url(container, source_url: str) -> str:
    for link in container.select("a[href]"):
        url = link.get("href", "").strip()
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and "/wp-content/uploads/" not in parsed.path:
            return url
    return source_url


def parse_schedule(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for container in soup.select("div.td_text_columns_two_cols"):
        heading = container.find("h4")
        if heading is None:
            continue

        lines = [line.strip() for line in heading.get_text("\n").splitlines() if line.strip()]
        if len(lines) < 3:
            continue

        location = _parse_location(lines[0])
        if location is None:
            continue
        city, country_code = location

        try:
            event_date = datetime.strptime(lines[1], "%B %d, %Y").date().isoformat()
        except ValueError:
            continue

        venue = " ".join(lines[2:]).replace("\xa0", " ").strip()
        if not venue:
            continue

        description = "\n".join((TOUR_TITLE, lines[0], lines[1], venue))
        records.append(
            {
                "title": TOUR_TITLE,
                "date": event_date,
                "url": _event_url(container, source_url),
                "time_from": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description,
                "source_url": source_url,
                "source": SOURCE,
            }
        )

    return records


class DimashNewsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="dimashnews_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["date", "venue", "city", "title"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching event schedule API", event="crawler_url_fetch", url=API_URL)
        response = requests.get(API_URL, timeout=30)
        response.raise_for_status()
        payload = response.json()

        source_url = payload.get("link") or SOURCE_URL
        html = payload.get("content", {}).get("rendered", "")
        records = parse_schedule(html, source_url)
        log_message(
            "Parsed event schedule",
            event="crawler_scrape_completed",
            url=source_url,
            record_count=len(records),
        )
        return records


def main():
    DimashNewsCrawler().run()


if __name__ == "__main__":
    main()
