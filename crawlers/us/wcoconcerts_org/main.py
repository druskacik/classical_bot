import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Wisconsin Chamber Orchestra"
SOURCE_URL = "https://wcoconcerts.org/"
EVENTS_URL = urljoin(SOURCE_URL, "load/events")
# All performance categories exposed by the calendar.  Galas (99696) is
# intentionally excluded because it contains fundraising dinners rather than
# concerts.
CATEGORY_IDS = "9,11,10,21087,115659"
PAGE_SIZE = 100
TIMEOUT = 60
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
MONTHS = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December"
)
OCCURRENCE_RE = re.compile(
    rf"\b({MONTHS})\s+(\d{{1,2}}),\s+(\d{{4}})"
    r"(?:\s+[—-]\s+(\d{1,2})(?::(\d{2}))?\s*([AP]M))?\b",
    re.IGNORECASE,
)


def clean_text(value) -> str:
    if not value:
        return ""
    text = str(value).replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def fetch(session: requests.Session, url: str, **kwargs) -> requests.Response:
    response = session.get(url, timeout=TIMEOUT, **kwargs)
    response.raise_for_status()
    return response


def listing_urls(session: requests.Session, timespan: str) -> list[str]:
    urls = []
    offset = 0
    while True:
        response = fetch(
            session,
            EVENTS_URL,
            params={
                "blockmode": "column",
                "category": CATEGORY_IDS,
                "location": "",
                "season": "",
                "timespan": timespan,
                "timestart": f"{date.today().year}-{date.today().month}-1",
                "order": "eventDateAndTime",
                "sort": "asc",
                "limit": PAGE_SIZE,
                "offset": offset,
            },
        )
        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select(".row.event")
        for row in rows:
            link = next(
                (
                    node.get("href")
                    for node in row.select('a[href*="/events/"]')
                    if node.get("href")
                ),
                None,
            )
            if link:
                urls.append(urljoin(SOURCE_URL, link).split("#", 1)[0])
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return urls


def parse_occurrences(value: str) -> list[tuple[str, str | None]]:
    occurrences = []
    for month, day, year, hour, minute, meridiem in OCCURRENCE_RE.findall(value):
        try:
            event_date = datetime.strptime(
                f"{month} {day} {year}", "%B %d %Y"
            ).date().isoformat()
        except ValueError:
            continue
        time_from = None
        if hour:
            parsed_hour = int(hour) % 12
            if meridiem.upper().startswith("P"):
                parsed_hour += 12
            time_from = f"{parsed_hour:02d}:{int(minute or 0):02d}"
        occurrence = (event_date, time_from)
        if occurrence not in occurrences:
            occurrences.append(occurrence)
    return occurrences


def parse_city(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return clean_text(text.split(",", 1)[0])


def detail_city(soup: BeautifulSoup) -> str:
    location_node = soup.select_one(".venue-location")
    city = parse_city(
        location_node.get_text(" ", strip=True) if location_node else ""
    )
    if city:
        return city
    address_node = soup.select_one(".venue-address")
    address = clean_text(address_node.get_text("\n", strip=True) if address_node else "")
    match = re.search(r",\s*([^,\n]+?),\s*[A-Z]{2}\s+\d{5}\b", address, re.I)
    if not match:
        match = re.search(r"(?:^|\n)([^,\n]+?),?\s+[A-Z]{2}\s+\d{5}\b", address, re.I)
    return clean_text(match.group(1)) if match else ""


def detail_description(soup: BeautifulSoup) -> str | None:
    parts = []
    for block in soup.select(".section.content .block.text"):
        text = clean_text(block.get_text("\n", strip=True))
        if text and text not in parts:
            parts.append(text)
    return "\n\n".join(parts) or None


def parse_detail_page(soup: BeautifulSoup, url: str) -> list[dict]:
    title_node = soup.select_one("h1")
    date_node = soup.select_one(".datetime")
    venue_node = soup.select_one(".venue-title")
    title = clean_text(title_node.get_text(" ", strip=True) if title_node else "")
    venue = clean_text(venue_node.get_text(" ", strip=True) if venue_node else "")
    city = detail_city(soup)
    occurrences = parse_occurrences(
        date_node.get_text(" ", strip=True) if date_node else ""
    )
    if not title or not venue or not city or not occurrences:
        return []

    description = detail_description(soup)
    return [
        {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "venue": venue,
            "city": city,
            "country_code": "US",
            "description": description,
            "source_url": SOURCE_URL,
            "source": SOURCE,
        }
        for event_date, time_from in occurrences
    ]


def fetch_detail(url: str) -> list[dict]:
    session = make_session()
    try:
        soup = BeautifulSoup(fetch(session, url).text, "html.parser")
        return parse_detail_page(soup, url)
    finally:
        session.close()


class WcoconcertsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="wcoconcerts_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        columns=[
            "title", "date", "url", "time_from", "venue", "city",
            "country_code", "description", "source_url", "source",
        ],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        session = make_session()
        try:
            urls = listing_urls(session, "past") + listing_urls(session, "upcoming")
        except requests.RequestException as error:
            log_message(
                "Failed to fetch Wisconsin Chamber Orchestra event listings",
                event="crawler_listing_request_failed",
                level="error",
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        urls = list(dict.fromkeys(urls))
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Skipping unavailable Wisconsin Chamber Orchestra event",
                        event="crawler_detail_request_failed",
                        level="warning",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        log_message(
            "Wisconsin Chamber Orchestra events parsed",
            event="crawler_parse_completed",
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda record: (record["date"], record["time_from"] or "", record["title"]),
        )


def main():
    WcoconcertsOrgCrawler().run()


if __name__ == "__main__":
    main()
