import re
from datetime import datetime
from html import unescape
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Charles Wuorinen"
SOURCE_URL = "https://www.charleswuorinen.com/"
FEED_URL = f"{SOURCE_URL}events/rdf.xml"

RSS_NS = "http://purl.org/rss/1.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"


def _text(element, namespace, name):
    child = element.find(f"{{{namespace}}}{name}")
    return child.text.strip() if child is not None and child.text else ""


def _plain_text(html):
    soup = BeautifulSoup(unescape(html or ""), "html.parser")
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip() or None


def _event_title(feed_title):
    title = feed_title.split(" | ", 1)[0].strip()
    title = re.sub(
        r"^\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s+(?:AM|PM)"
        r"(?:\s+Additional performances[^-]+)?\s+-\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return " ".join(title.split())


def _location(feed_title, description):
    """Return only locations directly stated by this small composer archive."""
    location = feed_title.split(" | ", 1)[1].splitlines()[0].strip() if " | " in feed_title else ""
    evidence = f"{feed_title}\n{description or ''}".lower()

    if "coolidge auditorium" in evidence and "library of congress" in evidence:
        return "Coolidge Auditorium, Library of Congress", "Washington"

    city_markers = (
        ("92nd street y", "New York"),
        ("houston", "Houston"),
        ("boston", "Boston"),
        ("santa cruz", "Santa Cruz"),
        ("brooklyn", "Brooklyn"),
        ("new york", "New York"),
        ("nyc", "New York"),
    )
    city = next((city for marker, city in city_markers if marker in evidence), None)

    # A street address is not a venue. Unknown locations are skipped below.
    if not location or re.match(r"^\d+\s", location):
        return None, city
    location = re.sub(r",\s*\d{1,5}\s+.*$", "", location).strip()
    return location or None, city


class CharlesWuorinenCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="charleswuorinen_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching event feed", event="crawler_url_fetch", url=FEED_URL)
        response = requests.get(FEED_URL, timeout=30)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)

        records = []
        for item in root.findall(f"{{{RSS_NS}}}item"):
            feed_title = _text(item, RSS_NS, "title")
            url = _text(item, RSS_NS, "link")
            date_text = _text(item, DC_NS, "date")
            description = _plain_text(_text(item, CONTENT_NS, "encoded"))
            if not feed_title or not url or not date_text:
                continue

            try:
                starts_at = datetime.fromisoformat(date_text)
            except ValueError:
                log_message(
                    "Skipping event with invalid date",
                    event="crawler_record_skipped",
                    url=url,
                    error_type="ValueError",
                    error_message="Invalid event date",
                )
                continue

            venue, city = _location(feed_title, description)
            if not venue or not city:
                log_message(
                    "Skipping event without a defensible venue or city",
                    event="crawler_record_skipped",
                    url=url,
                )
                continue

            records.append(
                {
                    "title": _event_title(feed_title),
                    "date": starts_at.date().isoformat(),
                    "url": url,
                    "time_from": starts_at.strftime("%H:%M"),
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "description": description,
                }
            )

        log_message(
            "Event feed parsed",
            event="crawler_scrape_completed",
            url=FEED_URL,
            record_count=len(records),
        )
        return records


def main():
    CharlesWuorinenCrawler().run()


if __name__ == "__main__":
    main()
