import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Seattle Chamber Music Society"
SOURCE_URL = "https://www.seattlechambermusic.org/"
EVENTS_API = f"{SOURCE_URL}wp-json/wp/v2/events"
HEADERS = {
    "Accept": "application/json, text/html;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0; "
        "+https://www.seattlechambermusic.org/)"
    ),
}

# SCMS presents almost all of its events in Seattle. The exceptions in its
# archive use these venue names, allowing the city to be inferred without
# mistaking the organization's home city for a touring location.
VENUE_CITIES = {
    "Bainbridge Island Museum of Art": "Bainbridge Island",
    "Center for Chamber Music": "Seattle",
    "Nordstrom Recital Hall at Benaroya Hall": "Seattle",
    "Seattle Art Museum": "Seattle",
    "Volunteer Park": "Seattle",
}


def _get(session, url, **kwargs):
    log_message("Fetching crawler URL", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=30, **kwargs)
    response.raise_for_status()
    return response


def _session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def _event_posts(session):
    posts = []
    page = 1
    while True:
        response = _get(
            session,
            EVENTS_API,
            params={"per_page": 100, "page": page, "orderby": "id", "order": "asc"},
        )
        posts.extend(response.json())
        total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    return posts


def _text(soup, selector):
    element = soup.select_one(selector)
    return element.get_text(" ", strip=True) if element else None


def _city_for(venue, page_text):
    if venue in VENUE_CITIES:
        return VENUE_CITIES[venue]
    for city in ("Seattle", "Bainbridge Island", "Bellevue", "Kirkland", "Redmond"):
        if re.search(rf"\b{re.escape(city)}(?:,\s*WA)?\b", page_text, re.IGNORECASE):
            return city
    return None


def _parse_event(session, post):
    url = post.get("link")
    if not url:
        return None
    try:
        soup = BeautifulSoup(_get(session, url).text, "html.parser")
        date_text = _text(soup, ".scms_streaming_the_program_meta_cal span")
        venue = _text(soup, ".scms_streaming_the_program_meta_loc span")
        title_node = soup.select_one("h1")
        if not date_text or not venue or not title_node:
            return None

        event_date = datetime.strptime(date_text, "%B %d, %Y").date().isoformat()
        time_text = _text(soup, ".scms_streaming_the_program_meta_dat span")
        time_from = datetime.strptime(time_text, "%I:%M %p").time().isoformat() if time_text else None

        content = soup.select_one(".events_columns_left")
        description = content.get_text("\n", strip=True) if content else None
        event_text = content.get_text(" ", strip=True) if content else ""
        city = _city_for(venue, event_text)
        if not city:
            return None

        return {
            "title": html.unescape(title_node.get_text(" ", strip=True)),
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
    except (requests.RequestException, ValueError) as error:
        log_message(
            "Skipping event detail",
            event="crawler_event_skipped",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


class SeattleChamberMusicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="seattlechambermusic_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        session = _session()
        posts = _event_posts(session)
        records = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_parse_event, session, post) for post in posts]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)
        records.sort(key=lambda record: (record["date"], record["time_from"] or "", record["title"]))
        log_message(
            "Parsed event details",
            event="crawler_events_parsed",
            record_count=len(records),
            candidate_count=len(posts),
        )
        return records


def main():
    SeattleChamberMusicCrawler().run()


if __name__ == "__main__":
    main()
