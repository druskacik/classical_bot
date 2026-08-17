import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Shreveport Symphony Orchestra"
SOURCE_URL = "https://www.shreveportsymphony.com/"
EVENTS_URL = "https://www.shreveportsymphony.com/upcoming-events"
DEFAULT_VENUE = "Riverview Theatre"
DEFAULT_CITY = "Shreveport"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_text(value):
    if not value:
        return None
    text = str(value).replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text).strip(" \ufeff")
    return text or None


def parse_occurrence(value):
    value = clean_text(value)
    if not value:
        return None, None
    try:
        parsed = datetime.strptime(value, "%B %d, %Y %I:%M %p")
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime("%H:%M")


def valid_event_url(value):
    if not value:
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "shreveportsymphony.app.getcuebox.com"
        and re.fullmatch(r"/o/ZMGVC75K/shows/[A-Za-z0-9]+/?", parsed.path) is not None
    )


def parse_events_page(html):
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for link in soup.select('a[href*="shreveportsymphony.app.getcuebox.com/o/ZMGVC75K/shows/"]'):
        url = link.get("href")
        if not valid_event_url(url):
            continue

        card = link.find_parent(class_="group")
        if card is None:
            continue

        date_node = card.find("h4")
        title_node = card.find("h3")
        description_node = card.find("p")
        event_date, time_from = parse_occurrence(
            date_node.get_text(" ", strip=True) if date_node else None
        )
        title = clean_text(title_node.get_text(" ", strip=True) if title_node else None)
        description = clean_text(
            description_node.get_text(" ", strip=True) if description_node else None
        )

        if not title or not event_date:
            log_message(
                "Skipping event card with missing title or invalid date",
                event="crawler_record_skipped",
                url=url,
                error_type="InvalidEventCard",
                error_message="Required title or date was not parseable",
            )
            continue

        records.append(
            {
                "title": title,
                "date": event_date,
                "url": url,
                "time_from": time_from,
                "venue": DEFAULT_VENUE,
                "city": DEFAULT_CITY,
                "description": description,
            }
        )

    return records


class ShreveportSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="shreveportsymphony_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching concert listing", event="crawler_url_fetch", url=EVENTS_URL)
        try:
            response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                "Failed to fetch concert listing",
                event="crawler_listing_request_failed",
                level="error",
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = parse_events_page(response.text)
        log_message(
            "Concert listing parsed",
            event="crawler_scrape_completed",
            url=EVENTS_URL,
            record_count=len(records),
        )
        return records


def main():
    ShreveportSymphonyComCrawler().run()


if __name__ == "__main__":
    main()
