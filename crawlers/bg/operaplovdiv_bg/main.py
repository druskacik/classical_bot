import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://operaplovdiv.bg/"
PROGRAM_URL = urljoin(SOURCE_URL, "bg/site/program")
SOURCE = "Opera Plovdiv"
REQUEST_TIMEOUT = 30


def clean_text(element):
    if element is None:
        return None
    text = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
    return text or None


class OperaPlovdivCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="operaplovdiv_bg",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="BG",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "classical-bot/1.0 (+https://github.com/markokajzer/classical-bot)"}
        )

    def _get_soup(self, url):
        log_message("Fetching Opera Plovdiv page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return BeautifulSoup(response.content, "html.parser")

    def _description(self, url):
        try:
            soup = self._get_soup(url)
        except requests.RequestException as error:
            log_message(
                "Could not fetch Opera Plovdiv event detail",
                event="crawler_url_fetch_failed",
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

        # This is the long editorial synopsis. Deliberately exclude cast,
        # ticket, location and other widgets surrounding it.
        return clean_text(soup.select_one("#details .rich-text"))

    def scrape(self):
        soup = self._get_soup(PROGRAM_URL)
        records = []

        # The desktop cards are repeated and omit the year. The compact
        # programme rows contain an explicit YYYY-Month data-id, including
        # still-published past performances in the current season.
        for card in soup.select('.card.event[data-id^="20"]'):
            link = card.select_one('a[href*="/performance/details"]')
            title = clean_text(card.select_one(".card-txts .title"))
            date_text = clean_text(card.select_one(".date"))
            time_from = clean_text(card.select_one(".time"))
            venue = clean_text(card.select_one(".location span"))
            year_match = re.match(r"(\d{4})-", card.get("data-id", ""))

            if not all((link, title, date_text, venue, year_match)):
                continue

            date_match = re.fullmatch(r"(\d{1,2})\s*/\s*(\d{1,2})", date_text)
            if not date_match:
                continue
            try:
                event_date = datetime(
                    int(year_match.group(1)),
                    int(date_match.group(2)),
                    int(date_match.group(1)),
                ).date().isoformat()
            except ValueError:
                continue

            url = urljoin(SOURCE_URL, link.get("href"))
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": time_from,
                    "venue": venue,
                    "city": "Plovdiv",
                    "description": self._description(url),
                }
            )

        log_message(
            "Opera Plovdiv programme parsed",
            event="crawler_records_parsed",
            record_count=len(records),
            url=PROGRAM_URL,
        )
        return records


def main():
    OperaPlovdivCrawler().run()


if __name__ == "__main__":
    main()
