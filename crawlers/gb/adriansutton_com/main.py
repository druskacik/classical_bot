import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Adrian Sutton"
SOURCE_URL = "https://www.adriansutton.com/"
EVENTS_URL = urljoin(SOURCE_URL, "news-and-events/")
TIMEOUT = 30

# These are venue-specific first-party calendars linked by the event copy.  The
# general orchestra links on some older cards are deliberately not treated as
# location evidence.
VENUE_CALENDARS = {
    "www.lakesidearts.org.uk": ("Djanogly Recital Hall", "Nottingham", "GB"),
    "skiptonmusic.org.uk": ("Skipton Town Hall", "Skipton", "GB"),
    "stollerhall.com": ("The Stoller Hall", "Manchester", "GB"),
    "www.lincolnsinn.org.uk": ("Lincoln's Inn", "London", "GB"),
    "www.rncm.ac.uk": ("Royal Northern College of Music", "Manchester", "GB"),
    "www.kino-teatr.co.uk": ("Kino-Teatr", "St Leonards-on-Sea", "GB"),
}

# The listing itself names these venues.  Keeping the patterns here also makes
# the parser useful when a card has no outbound event link.
TEXT_LOCATIONS = (
    (re.compile(r"St\.?\s+Alfege Church,\s*Greenwich", re.I),
     ("St Alfege Church", "London", "GB")),
    (re.compile(r"at Lincoln(?:’|'|`)s Inn", re.I),
     ("Lincoln's Inn", "London", "GB")),
    (re.compile(r"at (?:the )?RNCM", re.I),
     ("Royal Northern College of Music", "Manchester", "GB")),
)


def clean_text(node):
    if node is None:
        return None
    value = " ".join(node.get_text(" ", strip=True).split())
    return value or None


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), "%d/%m/%y").date().isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def event_url(card):
    links = []
    for anchor in card.select("a[href]"):
        url = urljoin(EVENTS_URL, anchor.get("href", ""))
        if urlparse(url).netloc and "/works/" not in url:
            links.append(url)
    return links[-1] if links else EVENTS_URL


def location_for(card, url):
    text = clean_text(card) or ""
    for pattern, location in TEXT_LOCATIONS:
        if pattern.search(text):
            return location

    hostname = urlparse(url).netloc.lower()
    return VENUE_CALENDARS.get(hostname)


def parse_events(html):
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for title_node in soup.select("div[x-show=\"showing === 'events'\"] h4"):
        card = title_node.find_parent("div", class_="bg-primary")
        if card is None:
            continue

        date_node = card.find("p", class_=lambda value: value and "font-bold" in value)
        event_date = parse_date(clean_text(date_node))
        title = clean_text(title_node)
        description_node = card.find("span")
        description = clean_text(description_node)
        url = event_url(card)
        location = location_for(card, url)
        if not title or not event_date or location is None:
            log_message(
                "Skipping event without a defensible date, venue, or city",
                event="crawler_event_skipped",
                url=url,
                error_type="IncompleteEvent",
            )
            continue

        venue, city, country_code = location
        records.append({
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        })

    return records


class AdrianSuttonComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="adriansutton_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GB",
        upload_target="classical",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching events page", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(
            EVENTS_URL,
            timeout=TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
        )
        response.raise_for_status()
        records = parse_events(response.text)
        log_message(
            "Parsed events page",
            event="crawler_page_parsed",
            url=response.url,
            record_count=len(records),
        )
        return records


def main():
    AdrianSuttonComCrawler().run()


if __name__ == "__main__":
    main()
