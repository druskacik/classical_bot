import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Pablo Center at the Confluence"
SOURCE_URL = "https://www.pablocenter.org/"
FEED_URL = SOURCE_URL + "multicategory/category_json/{offset}"
DEFAULT_VENUE = "Pablo Center at the Confluence"
DEFAULT_CITY = "Eau Claire"
PAGE_SIZE = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _clean_text(node):
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _parse_time(value):
    value = re.sub(r"\s+", " ", value or "").strip().upper()
    for pattern in ("%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(value, pattern).strftime("%H:%M")
        except ValueError:
            pass
    return None


def _valid_detail_url(value):
    parsed = urlparse(value or "")
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"www.pablocenter.org", "pablocenter.org"}
        and parsed.path.startswith("/events/detail/")
    )


def _feed_cards(session):
    cards = []
    offset = 0
    while True:
        url = FEED_URL.format(offset=offset)
        response = session.get(
            url,
            params={
                "category": 0,
                "venue": 0,
                "team": 0,
                "exclude": "",
                "per_page": PAGE_SIZE,
                "came_from_page": "event-list-page",
            },
            timeout=45,
        )
        response.raise_for_status()
        try:
            fragment = response.json()
        except requests.exceptions.JSONDecodeError as error:
            raise ValueError("Event feed did not return its expected JSON string") from error
        page_cards = BeautifulSoup(fragment, "html.parser").select(".eventItem")
        cards.extend(page_cards)
        if len(page_cards) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return cards


def _card_summary(card):
    link = card.select_one("h3.title a[href]")
    title = _clean_text(link)
    url = link.get("href", "").strip() if link else ""
    date_text = _clean_text(card.select_one(".date .m-date__singleDate"))
    date_text = re.sub(r"\s+,", ",", date_text)
    try:
        listing_date = datetime.strptime(date_text, "%b %d, %Y").date()
    except ValueError:
        listing_date = None
    return title, url, listing_date


def _showings(soup, fallback_date):
    years = re.findall(r"\b(20\d{2})\b", _clean_text(soup.select_one(".sidebar_event_date")))
    year = int(years[-1]) if years else (fallback_date.year if fallback_date else None)
    results = []
    for item in soup.select(".showings_date"):
        row = item.parent
        month = _clean_text(item.select_one(".m-date__month"))
        day = _clean_text(item.select_one(".m-date__day"))
        if not year or not month or not day:
            continue
        try:
            event_date = datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date()
        except ValueError:
            continue
        time_from = _parse_time(_clean_text(row.select_one(".time")))
        results.append((event_date.isoformat(), time_from))
    if results:
        return results
    if fallback_date:
        return [(fallback_date.isoformat(), None)]
    return []


def _parse_detail(summary):
    title, url, listing_date = summary
    if not title or not _valid_detail_url(url):
        return []

    session = _session()
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    page_title = _clean_text(soup.select_one("h1.title")) or title
    description_node = soup.select_one(".event_description")
    description = description_node.get_text("\n", strip=True) if description_node else None
    if description:
        description = re.sub(r"[ \t]+", " ", description).strip()

    venue = _clean_text(soup.select_one(".venue-title")) or DEFAULT_VENUE
    return [
        {
            "title": page_title,
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "venue": venue,
            "city": DEFAULT_CITY,
            "country_code": "US",
            "description": description,
            "source_url": SOURCE_URL,
            "source": SOURCE,
        }
        for event_date, time_from in _showings(soup, listing_date)
    ]


def scrape_events(session=None):
    session = session or _session()
    cards = _feed_cards(session)
    summaries = [_card_summary(card) for card in cards]
    records = []
    skipped = 0

    # Detail pages provide full descriptions and every performance time. A
    # small pool keeps the scrape practical without flooding the venue site.
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_parse_detail, summary): summary for summary in summaries}
        for future in as_completed(futures):
            summary = futures[future]
            try:
                parsed = future.result()
            except requests.RequestException as error:
                skipped += 1
                log_message(
                    "Event detail request failed",
                    event="crawler_detail_failed",
                    level="warning",
                    url=summary[1],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if not parsed:
                skipped += 1
            records.extend(parsed)

    if skipped:
        log_message(
            "Skipped events missing required detail data",
            event="crawler_records_skipped",
            level="warning",
            url=SOURCE_URL + "events",
            record_count=skipped,
        )
    log_message(
        "Pablo Center event feed parsed",
        event="crawler_scrape_completed",
        url=SOURCE_URL + "events",
        record_count=len(records),
    )
    return sorted(records, key=lambda item: (item["date"], item["time_from"] or "", item["title"]))


class PabloCenterOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="pablocenter_org",
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
        return scrape_events()


def main():
    PabloCenterOrgCrawler().run()


if __name__ == "__main__":
    main()
