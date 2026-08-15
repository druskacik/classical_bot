import gc
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, SoupStrainer
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.thailandphil.com/"
SOURCE = "Thailand Philharmonic Orchestra"
EVENTS_URL = urljoin(SOURCE_URL, "events/")


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)"}
    )
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _clean_text(element) -> str:
    return re.sub(r"\n[ \t]*\n+", "\n", element.get_text("\n", strip=True)).strip()


def _parse_time(value: str) -> tuple[str | None, str | None]:
    matches = re.findall(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", value, re.IGNORECASE)
    parsed = [datetime.strptime(item.upper(), "%I:%M %p").strftime("%H:%M") for item in matches]
    return (parsed[0] if parsed else None, parsed[1] if len(parsed) > 1 else None)


def _listing_page(session: requests.Session, page_number: int) -> BeautifulSoup:
    params = {
        "scope": "all",
        "mode": "monthly",
        "header_format": "<h2>#s</h2>",
        "date_format": "F Y",
        "pno": page_number,
        "action": "search_events_grouped",
    }
    response = session.get(EVENTS_URL, params=params, timeout=60)
    response.raise_for_status()
    content = response.content
    events_marker = content.find(b"css-events-list")
    if events_marker < 0:
        return BeautifulSoup("", "html.parser")
    start = content.rfind(b"<div", 0, events_marker)
    pagination_marker = content.find(b"em-pagination", events_marker)
    if pagination_marker >= 0:
        end = min(len(content), pagination_marker + 4000)
    else:
        end = content.find(b"</div>", events_marker) + len(b"</div>")
    # The theme emits several megabytes of unrelated inline assets before the
    # calendar. Parse only the compact calendar fragment.
    fragment = content[start:end]
    response.close()
    del response, content
    return BeautifulSoup(fragment, "html.parser")


def _get_listing_records(session: requests.Session) -> list[dict]:
    records = []
    page_number = 1
    while True:
        soup = _listing_page(session, page_number)
        anchors = soup.select(".css-events-list .events > a[name]")
        for anchor in anchors:
            title_element = anchor.select_one(".event_details h4")
            time_element = anchor.select_one(".event_time .time")
            raw_date = (anchor.get("name") or "").strip()
            try:
                parsed_date = date.fromisoformat(raw_date).isoformat()
            except ValueError:
                continue
            url = urljoin(EVENTS_URL, anchor.get("href", ""))
            if not title_element or not url:
                continue
            time_from, time_to = _parse_time(
                time_element.get_text(" ", strip=True) if time_element else ""
            )
            records.append(
                {
                    "title": title_element.get_text(" ", strip=True),
                    "date": parsed_date,
                    "url": url,
                    "time_from": time_from,
                    "time_to": time_to,
                }
            )

        has_next = soup.select_one(".em-pagination a.next") is not None
        has_anchors = bool(anchors)
        soup.decompose()
        del soup
        gc.collect()
        if not has_next or not has_anchors:
            break
        page_number += 1
    return records


def _venue_and_city(description: str) -> tuple[str | None, str | None]:
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    joined = "\n".join(lines)

    known_venues = (
        ("Prince Mahidol Hall", "Salaya"),
        ("Music Auditorium, College of Music", "Salaya"),
        ("MACM", "Salaya"),
    )
    for venue, city in known_venues:
        if re.search(rf"(?im)^{re.escape(venue)}(?:\s.*)?$", joined):
            return venue, city

    for index, line in enumerate(lines):
        country_match = re.fullmatch(r"([^,\n]+),\s*Thailand", line, re.IGNORECASE)
        if not country_match or index == 0:
            continue
        venue = lines[index - 1]
        city = country_match.group(1).strip()
        if venue != city and not re.search(r"\b(?:am|pm)\b", venue, re.IGNORECASE):
            return venue, city

    # Most archive entries are home concerts and older pages often omit their
    # venue. Touring/outreach pages must provide an explicit place to be usable.
    if not re.search(r"\b(?:tour|touring|outreach|abroad)\b", description, re.IGNORECASE):
        return "Prince Mahidol Hall", "Salaya"
    return None, None


def _detail(session: requests.Session, record: dict) -> dict | None:
    response = session.get(record["url"], timeout=60)
    response.raise_for_status()
    body = response.content
    marker = body.find(b"entry-content")
    if marker < 0:
        return None
    start = body.rfind(b"<div", 0, marker)
    fragment = body[start:]
    response.close()
    del response, body
    content_only = SoupStrainer("div", class_="entry-content")
    soup = BeautifulSoup(fragment, "html.parser", parse_only=content_only)
    content = soup.select_one(".entry-content")
    if not content:
        return None
    description = _clean_text(content)
    soup.decompose()
    venue, city = _venue_and_city(description)
    if not venue or not city:
        return None
    return {
        **record,
        "venue": venue,
        "city": city,
        "description": description or None,
    }


class ThailandPhilCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="thailandphil_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="TH",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        session = _session()
        listings = _get_listing_records(session)
        records = []
        # Pages contain unusually large theme assets inline, so keep parsing
        # concurrency modest even though only .entry-content is retained.
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_detail, session, item): item for item in listings}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        "Failed to fetch concert detail",
                        event="crawler_url_fetch_failed",
                        url=item["url"],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["url"]))
        log_message(
            "Concert details collected",
            event="crawler_details_collected",
            record_count=len(records),
        )
        return records


def main():
    ThailandPhilCrawler().run()


if __name__ == "__main__":
    main()
