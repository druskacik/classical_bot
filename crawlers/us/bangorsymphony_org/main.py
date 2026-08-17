import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Bangor Symphony Orchestra"
SOURCE_URL = "https://www.bangorsymphony.org/"
SCHEDULE_URL = urljoin(SOURCE_URL, "schedule/")
BSYO_URL = urljoin(SOURCE_URL, "bsyo/calendar/")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "Accept-Language": "en-US,en;q=0.9",
}

VENUE_CITIES = {
    "Collins Center for the Arts": "Orono",
    "Peakes Auditorium at Bangor High School": "Bangor",
}

DATE_TIME_RE = re.compile(
    r"([A-Z][a-z]+ \d{1,2}, \d{4})\s+at\s+"
    r"(\d{1,2}(?::\d{2})?\s*[ap]m)\b",
    re.IGNORECASE,
)
BSYO_CONCERT_RE = re.compile(
    r"((?:Fall|Spring) Concert):\s*"
    r"([A-Z][a-z]+ \d{1,2}, \d{4})\s+at\s+"
    r"(\d{1,2}(?::\d{2})?\s*[ap]m)\.?\s*"
    r"([^\n,]+?)(?:,\s*\d|\n)",
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    normalized = clean_text(value).upper().replace(" ", "")
    for pattern in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(normalized, pattern).strftime("%H:%M")
        except ValueError:
            pass
    return None


def valid_show_url(url):
    parsed = urlparse(url or "")
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"bangorsymphony.org", "www.bangorsymphony.org"}
        and parsed.path.startswith("/show/")
    )


def fetch_soup(session, url):
    log_message("Fetching concert page", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def show_records(soup, url):
    title_node = soup.select_one("h1.entry-title")
    content = soup.select_one(".entry-content")
    location = soup.select_one(".show-date-location")
    date_nodes = location.select(".show-date") if location else []
    title = clean_text(title_node.get_text(" ", strip=True) if title_node else "")

    if not title or not content or not location or not valid_show_url(url):
        return []

    location_copy = BeautifulSoup(str(location), "html.parser")
    for node in location_copy.select(".show-date"):
        node.decompose()
    venue = clean_text(location_copy.get_text(" ", strip=True))
    city = VENUE_CITIES.get(venue)
    if not venue or not city:
        log_message(
            "Skipping show with unknown venue",
            event="crawler_record_skipped",
            level="warning",
            url=url,
            error_type="UnknownVenue",
            error_message=venue or "missing venue",
        )
        return []

    description = clean_text(content.get_text("\n", strip=True)) or None
    records = []
    for node in date_nodes:
        match = DATE_TIME_RE.fullmatch(clean_text(node.get_text(" ", strip=True)))
        if not match:
            continue
        event_date = parse_date(match.group(1))
        time_from = parse_time(match.group(2))
        if event_date and time_from:
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


def bsyo_records(soup):
    content = soup.select_one(".entry-content")
    if not content:
        return []
    text = content.get_text("\n", strip=True).replace("\xa0", " ")
    description = clean_text(text) or None
    records = []
    for match in BSYO_CONCERT_RE.finditer(text):
        venue = clean_text(match.group(4))
        city = VENUE_CITIES.get(venue)
        event_date = parse_date(match.group(2))
        time_from = parse_time(match.group(3))
        if not all((venue, city, event_date, time_from)):
            continue
        records.append(
            {
                "title": f"BSYO {clean_text(match.group(1)).title()}",
                "date": event_date,
                "url": BSYO_URL,
                "time_from": time_from,
                "venue": venue,
                "city": city,
                "description": description,
            }
        )
    return records


class BangorSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="bangorsymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        schedule = fetch_soup(session, SCHEDULE_URL)

        show_urls = []
        for link in schedule.select(".show-list-item a.home-show[href]"):
            url = urljoin(SCHEDULE_URL, link.get("href"))
            if valid_show_url(url) and url not in show_urls:
                show_urls.append(url)

        records = []
        for url in show_urls:
            records.extend(show_records(fetch_soup(session, url), url))
        records.extend(bsyo_records(fetch_soup(session, BSYO_URL)))

        log_message(
            "Concert pages parsed",
            event="crawler_scrape_completed",
            url=SCHEDULE_URL,
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda item: (item["date"], item["time_from"] or "", item["title"]),
        )


def main():
    BangorSymphonyOrgCrawler().run()


if __name__ == "__main__":
    main()
