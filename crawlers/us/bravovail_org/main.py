import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Bravo! Vail Music Festival"
SOURCE_URL = "https://www.bravovail.org/"
EVENT_FEED_URL = "https://d15m308py1w27x.cloudfront.net/Prod/event-feed/6/live"

HEADERS = {
    "Accept": "application/json, text/html;q=0.9",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
}

# The festival uses a small, stable group of named venues in the Vail Valley.
# Only exact venue matches are defaulted; an unfamiliar or touring venue is
# skipped unless its city is explicitly included in the venue string.
VENUE_CITIES = {
    "Gerald R. Ford Amphitheater": "Vail",
    "Gerald R Ford Amphitheater": "Vail",
    "Vilar Performing Arts Center": "Beaver Creek",
    "Donovan Pavilion": "Vail",
    "Vail Interfaith Chapel": "Vail",
    "Vail Chapel": "Vail",
    "Avon Performance Pavilion": "Avon",
    "Nottingham Park": "Avon",
    "Eagle-Vail Pavilion": "Eagle-Vail",
    "Bol in Vail": "Vail",
    "Chasing Rabbits, Vail": "Vail",
}


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def valid_event_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"bravovail.org", "www.bravovail.org"}
        and parsed.path.startswith("/performances/")
    )


def parse_datetime(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None, None
    time_from = parsed.strftime("%H:%M") if parsed.time() != datetime.min.time() else None
    return parsed.date().isoformat(), time_from


def venue_and_city(item, soup):
    suffix_node = soup.select_one(".concert-header__suffix")
    detail_venue = clean_text(suffix_node.get_text(" ", strip=True) if suffix_node else "")
    facility = item.get("facility")
    if isinstance(facility, dict):
        facility = facility.get("name") or facility.get("description")
    venue = clean_text(facility) or detail_venue or clean_text(item.get("suffix"))
    if not venue:
        return None, None

    city = VENUE_CITIES.get(venue)
    if not city and "," in venue:
        left, possible_city = (clean_text(part) for part in venue.rsplit(",", 1))
        if left and possible_city and re.fullmatch(r"[A-Za-z][A-Za-z .'-]+", possible_city):
            venue, city = left, possible_city
    return venue or None, city


def description_from_page(soup):
    sections = []
    for node in soup.select(".content-panel__body.s-prose"):
        text = clean_text(node.get_text("\n", strip=True))
        if text and text not in sections:
            sections.append(text)
    return "\n\n".join(sections) or None


def parse_event(item, session):
    url = clean_text(item.get("link"))
    title = clean_text(item.get("title"))
    event_date, time_from = parse_datetime(item.get("perf_date"))
    if not title or not event_date or not valid_event_url(url):
        return None

    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    venue, city = venue_and_city(item, soup)
    if not venue or not city:
        log_message(
            "Skipping event without a defensible venue and city",
            event="crawler_record_skipped",
            level="warning",
            url=url,
            error_type="MissingLocation",
            error_message="Venue or city unavailable",
        )
        return None

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": "US",
        "description": description_from_page(soup),
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


class BravoVailOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="bravovail_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "venue",
            "city",
            "country_code",
            "description",
            "source_url",
            "source",
        ],
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        log_message("Fetching event feed", event="crawler_url_fetch", url=EVENT_FEED_URL)
        response = session.get(EVENT_FEED_URL, timeout=45)
        response.raise_for_status()
        items = response.json()
        if not isinstance(items, list):
            raise ValueError("Bravo! Vail event feed did not return a list")

        records = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                record = parse_event(item, session)
            except requests.RequestException as error:
                log_message(
                    "Event detail request failed",
                    event="crawler_url_fetch_failed",
                    level="warning",
                    url=clean_text(item.get("link")),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["title"]))
        log_message(
            "Event feed parsed",
            event="crawler_scrape_completed",
            url=EVENT_FEED_URL,
            record_count=len(records),
        )
        return records


def main():
    BravoVailOrgCrawler().run()


if __name__ == "__main__":
    main()
