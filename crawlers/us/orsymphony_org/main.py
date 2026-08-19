import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.orsymphony.org/"
SOURCE = "Oregon Symphony"
SITEMAP_URL = urljoin(SOURCE_URL, "sitemap.xml")
REQUEST_TIMEOUT = 30
DATE_FORMAT = "%a, %b %d, %Y, %I:%M %p"


def _clean_text(element) -> str | None:
    if element is None:
        return None
    text = "\n".join(
        line.strip() for line in element.get_text("\n").splitlines() if line.strip()
    )
    return text or None


def _production_urls(session: requests.Session) -> list[str]:
    response = session.get(SITEMAP_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "xml")
    urls = {
        loc.get_text(strip=True).rstrip("/")
        for loc in soup.find_all("loc")
        if "/productions/" in loc.get_text()
    }
    return sorted(urls)


def _description(soup: BeautifulSoup) -> str | None:
    sections = []
    for selector in (".production-overview", ".production-information"):
        text = _clean_text(soup.select_one(selector))
        if text:
            sections.append(text)
    return "\n\n".join(sections) or None


def parse_production(html: bytes | str, url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    title_element = soup.select_one(".production-header h2")
    title = _clean_text(title_element)
    if not title:
        return []

    description = _description(soup)
    records = []
    for venue_group in soup.select(".production-dates__venue"):
        location = _clean_text(venue_group.select_one(".production-dates__venue-name span"))
        if not location or "," not in location:
            continue
        venue, city = (part.strip() for part in location.rsplit(",", 1))
        if not venue or not city:
            continue

        for label in venue_group.select("label"):
            date_text = _clean_text(label)
            if not date_text:
                continue
            date_text = re.sub(r"\s+", " ", date_text)
            try:
                start = datetime.strptime(date_text.title(), DATE_FORMAT)
            except ValueError:
                continue
            records.append(
                {
                    "title": title,
                    "date": start.date().isoformat(),
                    "url": url,
                    "time_from": start.time().strftime("%H:%M"),
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "description": description,
                }
            )
    return records


def _fetch_production(url: str) -> list[dict]:
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return parse_production(response.content, response.url.rstrip("/"))


class OregonSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="orsymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(
            {"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0; +https://www.orsymphony.org/)"}
        )
        urls = _production_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_fetch_production, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Concert detail fetch failed",
                        event="crawler_url_fetch_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return records


def main():
    OregonSymphonyCrawler().run()


if __name__ == "__main__":
    main()
