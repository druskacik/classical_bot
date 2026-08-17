import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://stcloudsymphony.com/"
SOURCE = "St. Cloud Symphony Orchestra"
EVENT_SITEMAP_URL = f"{SOURCE_URL}wp-sitemap-posts-edge-event-1.xml"
REQUEST_TIMEOUT = 30
HEADERS = {
    "User-Agent": "classical-bot/1.0 (+https://github.com/)",
}


def _clean_text(element) -> str:
    text = element.get_text("\n", strip=True)
    text = re.sub(r"\n\s*Warning:.*?(?=\n|$)", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_time(description: str) -> str | None:
    # Prefer the performance time over an earlier pre-concert talk.
    concert_match = re.search(
        r"\b(\d{1,2}:\d{2})\s*([ap])\.?m\.?\s+(?:Concert|Recital)\b",
        description,
        flags=re.IGNORECASE,
    )
    match = concert_match or re.search(
        r"\b(?:at\s+)?(\d{1,2}:\d{2})\s*([ap])\.?m\.?",
        description,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    hour, minute = (int(part) for part in match.group(1).split(":"))
    if match.group(2).lower() == "p" and hour != 12:
        hour += 12
    elif match.group(2).lower() == "a" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _parse_event(session: requests.Session, url: str) -> dict | None:
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    title_element = soup.select_one(".edgtf-event-title")
    date_element = soup.select_one(
        ".edgtf-event-date .edgtf-event-info-item-title"
    )
    location_element = soup.select_one(
        ".edgtf-event-location .edgtf-event-info-item-desc"
    )
    content_element = soup.select_one(".edgtf-event-content")

    if not all((title_element, date_element, location_element, content_element)):
        log_message(
            "Skipping event with missing required details",
            event="crawler_record_skipped",
            url=url,
        )
        return None

    date_label = date_element.get_text(" ", strip=True).rstrip(":").lower()
    date_value_element = date_element.find_next_sibling(
        class_="edgtf-event-info-item-desc"
    )
    if date_label != "date" or date_value_element is None:
        log_message(
            "Skipping event with missing date",
            event="crawler_record_skipped",
            url=url,
        )
        return None

    try:
        event_date = datetime.strptime(
            date_value_element.get_text(" ", strip=True), "%B %d, %Y"
        ).date().isoformat()
    except ValueError as error:
        log_message(
            "Skipping event with invalid date",
            event="crawler_record_skipped",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    venue = location_element.get_text(" ", strip=True)
    description = _clean_text(content_element)
    if not venue or not description:
        return None

    return {
        "title": title_element.get_text(" ", strip=True),
        "date": event_date,
        "url": url,
        "time_from": _parse_time(description),
        "time_to": None,
        "venue": venue,
        "city": "St. Cloud",
        "description": description,
    }


class StCloudSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="stcloudsymphony_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(HEADERS)

        log_message(
            "Fetching event sitemap",
            event="crawler_url_fetch",
            url=EVENT_SITEMAP_URL,
        )
        response = session.get(EVENT_SITEMAP_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        sitemap = BeautifulSoup(response.content, "xml")
        urls = [
            loc.get_text(strip=True)
            for loc in sitemap.find_all("loc")
            if "/event/" in loc.get_text(strip=True)
        ]

        records = []
        for url in urls:
            try:
                record = _parse_event(session, url)
            except requests.RequestException as error:
                log_message(
                    "Concert detail request failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
        return records


def main():
    StCloudSymphonyCrawler().run()


if __name__ == "__main__":
    main()
