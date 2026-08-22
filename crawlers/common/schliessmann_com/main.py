import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Burkard Schliessmann"
SOURCE_URL = "https://schliessmann.com/"
EVENT_PAGES = (
    urljoin(SOURCE_URL, "calendar.htm"),
    urljoin(SOURCE_URL, "news.htm"),
)

DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s*,?\s*(\d{1,2})\s*,\s*(2\s*0\s*\d\s*\d)\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*[–-]\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)


def clean_text(value):
    return re.sub(r"\s+", " ", value).strip()


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    value = f"{match.group(1)} {match.group(2)}, {match.group(3).replace(' ', '')}"
    try:
        return datetime.strptime(value, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def parse_times(text):
    match = TIME_RE.search(text)
    if not match:
        return None, None

    def convert(hour, minute, meridiem):
        parsed = datetime.strptime(
            f"{hour}:{minute or '00'} {meridiem.upper()}", "%I:%M %p"
        )
        return parsed.strftime("%H:%M")

    return (
        convert(match.group(1), match.group(2), match.group(3)),
        convert(match.group(4), match.group(5), match.group(6)),
    )


def extract_location(text):
    """Return only locations explicitly supported by an event block."""
    tampa = re.search(
        r"(The Music Gallery,\s*Steinway\s*&\s*Sons of Tampa Bay).*?"
        r"\bClearwater,\s*FL\s+USA\b",
        text,
        re.IGNORECASE,
    )
    if tampa:
        return clean_text(tampa.group(1)), "Clearwater", "US"

    if re.search(r"\bVilla Bonn\b", text, re.IGNORECASE) and re.search(
        r"\b(?:Francfort|Frankfurt)(?:/Main)?\b", text, re.IGNORECASE
    ):
        return "Villa Bonn", "Frankfurt am Main", "DE"

    return None


def is_performance(text):
    lowered = text.lower()
    positive = (
        "will perform",
        "will perform works",
        "concert with international",
        "presents this 3 cd-edition at an event",
    )
    excluded = (
        "broadcast program",
        "airs on sunday",
        "album of the week",
    )
    return any(term in lowered for term in positive) and not any(
        term in lowered for term in excluded
    )


def parse_event_box(box, page_url):
    text = clean_text(box.get_text(" ", strip=True))
    event_date = parse_date(text)
    location = extract_location(text)
    if not event_date or not location or not is_performance(text):
        return None

    heading = box.select_one(".caltitle")
    title = clean_text(heading.get_text(" ", strip=True)) if heading else ""
    if not title:
        return None

    time_from, time_to = parse_times(text)
    venue, city, country_code = location
    return {
        "title": title,
        "date": event_date,
        "url": page_url,
        "time_from": time_from,
        "time_to": time_to,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": text,
    }


class SchliessmannCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="schliessmann_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        session = requests.Session()
        session.headers["User-Agent"] = "classical-concert-crawler/1.0"

        for page_url in EVENT_PAGES:
            log_message("Fetching event page", event="crawler_url_fetch", url=page_url)
            try:
                response = session.get(page_url, timeout=30)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    "Event page fetch failed",
                    event="crawler_url_fetch_failed",
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.content, "html.parser")
            for box in soup.select(".calendarbox"):
                record = parse_event_box(box, page_url)
                if record:
                    records.append(record)

        log_message(
            "Event pages parsed",
            event="crawler_pages_parsed",
            record_count=len(records),
        )
        return records


def main():
    SchliessmannCrawler().run()


if __name__ == "__main__":
    main()
