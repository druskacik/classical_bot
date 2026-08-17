import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://promusicacolumbus.org/"
SOURCE = "ProMusica Chamber Orchestra"
SITEMAP_URL = f"{SOURCE_URL}sitemap_index.xml"

# ProMusica's venue pages and event calendar establish these as its Columbus-area
# performance venues. Unknown venues are deliberately skipped rather than given
# the orchestra's home city.
VENUE_CITIES = {
    "southern theatre": "Columbus",
    "saint mary catholic church": "Columbus",
    "st. mary catholic church": "Columbus",
    "worthington united methodist church": "Worthington",
    "franklin park conservatory & botanical gardens": "Columbus",
    "franklin park conservatory": "Columbus",
    "headley park": "Gahanna",
    "alum creek park amphitheater": "Westerville",
    "the fives": "Columbus",
    "first community north": "Columbus",
}

DATE_FORMATS = (
    "%A, %B %d, %Y | %I:%M %p",
    "%A, %B %d, %Y",
)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_occurrence(value: str) -> tuple[str, str | None] | None:
    value = clean_text(value)
    for date_format in DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, date_format)
            time_from = parsed.strftime("%H:%M") if "%I" in date_format else None
            return parsed.strftime("%Y-%m-%d"), time_from
        except ValueError:
            continue
    return None


class ProMusicaColumbusCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="promusicacolumbus_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "classical-concert-crawler/1.0 (+https://github.com/)"}
        )

    def get_soup(self, url: str) -> BeautifulSoup:
        log_message("Fetching page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        parser = "xml" if url == SITEMAP_URL else "html.parser"
        return BeautifulSoup(response.content, parser)

    def event_urls(self) -> list[str]:
        sitemap = self.get_soup(SITEMAP_URL)
        return sorted(
            {
                clean_text(location.get_text())
                for location in sitemap.find_all("loc")
                if "/event/" in location.get_text()
            }
        )

    def parse_event(self, url: str) -> list[dict]:
        soup = self.get_soup(url)
        content = soup.select_one(".content-column")
        title_node = content.select_one("h2") if content else None
        if not content or not title_node:
            log_message("Event content missing", event="crawler_event_skipped", url=url)
            return []

        title = clean_text(title_node.get_text(" ", strip=True))
        description_content = BeautifulSoup(str(content), "html.parser")
        for selector in (
            "h2",
            ".article-image",
            ".event-dates",
            ".event-info",
            ".tickets-link",
        ):
            for node in description_content.select(selector):
                node.decompose()
        description = description_content.get_text("\n", strip=True) or None

        records = []
        for occurrence in content.select(".event-date"):
            date_node = occurrence.select_one(".event-date-title")
            venue_node = occurrence.select_one(".event-location")
            if not date_node or not venue_node:
                continue

            parsed = parse_occurrence(date_node.get_text(" ", strip=True))
            venue = clean_text(venue_node.get_text(" ", strip=True))
            city = VENUE_CITIES.get(venue.casefold())
            if not parsed or not venue or not city:
                log_message(
                    "Occurrence missing required details",
                    event="crawler_occurrence_skipped",
                    url=url,
                    venue=venue or None,
                )
                continue

            event_date, time_from = parsed
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": time_from,
                    "venue": venue,
                    "city": city,
                    "description": description,
                }
            )
        return records

    def scrape(self) -> list[dict]:
        records = []
        for url in self.event_urls():
            try:
                records.extend(self.parse_event(url))
            except requests.RequestException as error:
                log_message(
                    "Event fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return records


def main():
    ProMusicaColumbusCrawler().run()


if __name__ == "__main__":
    main()
