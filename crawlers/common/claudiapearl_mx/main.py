import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Claudia Pearl"
SOURCE_URL = "https://claudiapearl.mx/en.html"
NEWS_URL = "https://claudiapearl.mx/news.html"
TIMEOUT = 30

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
DATE_RE = re.compile(
    rf"\b({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(\d{{4}})\b",
    re.IGNORECASE,
)


def _get_soup(url: str) -> BeautifulSoup:
    log_message("Fetching source page", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def _clean_text(element) -> str:
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def _parse_date(text: str) -> str | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    value = " ".join(match.groups())
    try:
        return datetime.strptime(value, "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def _location(text: str) -> tuple[str, str, str] | None:
    """Return only locations supported explicitly by the event copy."""
    if "London College of Music" in text:
        return "London College of Music", "London", "GB"
    return None


def _detail_urls(news_soup: BeautifulSoup) -> list[str]:
    urls = []
    for link in news_soup.select("ul.blog-posts h3 a[href]"):
        url = urljoin(NEWS_URL, link["href"])
        if urlparse(url).netloc == "claudiapearl.mx" and url not in urls:
            urls.append(url)
    return urls


def _parse_detail(url: str, soup: BeautifulSoup) -> dict | None:
    heading = soup.select_one(".page-title h1")
    content = soup.select_one(".blog-single .post-content-wrapper")
    if not heading or not content:
        return None

    title = _clean_text(heading)
    description = _clean_text(content)
    event_date = _parse_date(description)
    location = _location(description)
    if not title or not event_date or not location:
        return None

    venue, city, country_code = location
    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": None,
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class ClaudiaPearlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="claudiapearl_mx",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        news_soup = _get_soup(NEWS_URL)
        records = []
        for url in _detail_urls(news_soup):
            try:
                record = _parse_detail(url, _get_soup(url))
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch event detail",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
        return records


def main():
    ClaudiaPearlCrawler().run()


if __name__ == "__main__":
    main()
