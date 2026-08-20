import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Benjamin Lees"
SOURCE_URL = "https://www.benjaminlees.com/"
EVENTS_URL = f"{SOURCE_URL}projectsevents.html"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _sections(soup: BeautifulSoup):
    """Yield the text belonging to each heading on the legacy events page."""
    for container in soup.select("div.paragraph"):
        heading = None
        parts = []
        for child in container.children:
            if isinstance(child, Tag) and child.name == "strong":
                if heading:
                    yield _clean_text(heading), _clean_text(" ".join(parts))
                heading = child.get_text(" ", strip=True)
                parts = []
            elif heading:
                if isinstance(child, NavigableString):
                    parts.append(str(child))
                elif isinstance(child, Tag):
                    parts.append(child.get_text(" ", strip=True))
        if heading:
            yield _clean_text(heading), _clean_text(" ".join(parts))


def _parse_date(text: str):
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December) "
        r"(\d{1,2})(?:st|nd|rd|th)?, (\d{4})\b",
        text,
    )
    if not match:
        return None
    return datetime.strptime(
        f"{match.group(1)} {match.group(2)} {match.group(3)}", "%B %d %Y"
    ).date().isoformat()


def _parse_event(description: str):
    """Parse concrete performances that have a defensible date, city and venue."""
    date = _parse_date(description)
    if not date or not re.search(r"\b(?:premiered|performed)\b", description, re.I):
        return None

    title_match = re.search(r"^The (.+?) was premiered\b", description, re.I)
    venue_match = re.search(r"\bheld at (.+?)(?: with|\.|$)", description, re.I)
    city_match = re.search(
        r"\bConvention, ([A-Za-z .'-]+), (?:Arizona|AZ)\b", description
    )
    if not (title_match and venue_match and city_match):
        return None

    return {
        "title": _clean_text(title_match.group(1)),
        "date": date,
        "url": EVENTS_URL,
        "time_from": None,
        "venue": _clean_text(venue_match.group(1)),
        "city": _clean_text(city_match.group(1)),
        "country_code": "US",
        "description": description,
    }


class BenjaminLeesCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="benjaminlees_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching events page", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(EVENTS_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for _, description in _sections(soup):
            record = _parse_event(description)
            if record:
                records.append(record)

        log_message(
            "Events page parsed",
            event="crawler_scrape_completed",
            url=EVENTS_URL,
            record_count=len(records),
        )
        return records


def main():
    BenjaminLeesCrawler().run()


if __name__ == "__main__":
    main()
