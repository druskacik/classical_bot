from datetime import datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Pittsburgh Symphony Orchestra"
SOURCE_URL = "https://pittsburghsymphony.org/"
FEED_URL = "https://pittsburghsymphony.org/feed?format=rss"
DEFAULT_CITY = "Pittsburgh"


def _text(item, name):
    element = item.find(name)
    if element is None or element.text is None:
        return None
    value = " ".join(element.text.split())
    return value or None


def _valid_production_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"pittsburghsymphony.org", "www.pittsburghsymphony.org"}
        and parsed.path.startswith("/production/")
    )


class PittsburghSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="pittsburghsymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching concert feed", event="crawler_url_fetch", url=FEED_URL)
        response = requests.get(
            FEED_URL,
            timeout=45,
            headers={
                "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
                "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
            },
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)

        records = []
        for item in root.findall("./channel/item"):
            title = _text(item, "title")
            url = _text(item, "link")
            date_text = _text(item, "date")
            time_text = _text(item, "time")
            venue = _text(item, "venue")

            if not title or not _valid_production_url(url) or not date_text:
                continue
            if not venue or venue.casefold() == "see event description":
                continue

            try:
                event_date = datetime.strptime(date_text, "%a, %b %d, %Y").date().isoformat()
            except (TypeError, ValueError, OverflowError):
                log_message(
                    "Skipping event with invalid date",
                    event="crawler_record_skipped",
                    url=url,
                    error_type="InvalidDate",
                    error_message=date_text,
                )
                continue

            time_from = None
            if time_text:
                try:
                    time_from = datetime.strptime(time_text.upper(), "%I:%M%p").strftime("%H:%M:%S")
                except ValueError:
                    log_message(
                        "Event time could not be parsed",
                        event="crawler_field_parse_failed",
                        url=url,
                        error_type="InvalidTime",
                        error_message=time_text,
                    )

            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": time_from,
                    "time_to": None,
                    "venue": venue,
                    "city": DEFAULT_CITY,
                    "description": None,
                }
            )

        log_message(
            "Concert feed parsed",
            event="crawler_scrape_completed",
            url=FEED_URL,
            record_count=len(records),
        )
        return records


def main():
    PittsburghSymphonyCrawler().run()


if __name__ == "__main__":
    main()
