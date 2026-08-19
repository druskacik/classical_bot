import re
from datetime import date
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "New Texas Symphony Orchestra"
SOURCE_URL = "https://www.ntso.org/"
EVENTS_URL = urljoin(SOURCE_URL, "concerts")
DEFAULT_CITY = "Dallas"


def _clean_text(element) -> str | None:
    if element is None:
        return None
    text = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
    return text or None


def _time(element) -> str | None:
    text = _clean_text(element)
    if not text:
        return None
    text = text.replace("\u202f", " ").replace("\xa0", " ")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([AP]M)", text, re.IGNORECASE)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.upper() == "PM" else 0)
    return f"{hour:02d}:{minute}"


def _venue_and_city(article) -> tuple[str | None, str | None]:
    address = article.select_one(".eventlist-meta-address")
    if address is None:
        return None, None

    venue = " ".join(address.find_all(string=True, recursive=False)).strip()
    venue = re.sub(r"\s+", " ", venue) or None
    map_link = address.select_one("a[href]")
    map_query = ""
    if map_link:
        map_query = parse_qs(urlparse(map_link["href"]).query).get("q", [""])[0]

    city = DEFAULT_CITY if re.search(r"\bDallas\b", map_query, re.IGNORECASE) else None
    city_match = re.search(r",\s*([^,\d]+),\s*(?:Texas|TX)(?:,|$)", map_query, re.IGNORECASE)
    if city_match:
        city = city_match.group(1).strip()
    return venue, city or DEFAULT_CITY


def parse_events(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for article in soup.select("article.eventlist-event"):
        title_link = article.select_one(".eventlist-title-link")
        date_element = article.select_one(".eventlist-meta-date .event-date")
        venue, city = _venue_and_city(article)
        if title_link is None or date_element is None or not venue or not city:
            continue

        raw_date = date_element.get("datetime", "")[:10]
        try:
            event_date = date.fromisoformat(raw_date).isoformat()
        except ValueError:
            continue

        title = _clean_text(title_link)
        relative_url = title_link.get("href")
        if not title or not relative_url:
            continue

        description = _clean_text(article.select_one(".eventlist-description"))
        start = article.select_one(".event-time-localized-start")
        end = article.select_one(".event-time-localized-end")
        if start is None:
            times = article.select(".eventlist-meta-time time.event-time-localized")
            start = times[0] if times else None
            end = times[-1] if len(times) > 1 else None

        records.append(
            {
                "title": title,
                "date": event_date,
                "url": urljoin(SOURCE_URL, relative_url),
                "time_from": _time(start),
                "time_to": _time(end),
                "venue": venue,
                "city": city,
                "description": description,
            }
        )
    return records


def parse_detail_description(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".eventitem-column-content")
    if content is None:
        return None
    for unwanted in content.select("script, style, .eventitem-meta, .eventitem-backlink"):
        unwanted.decompose()
    return _clean_text(content)


class NtsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="ntso_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching event archive", event="crawler_url_fetch", url=EVENTS_URL)
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"})
        try:
            response = session.get(EVENTS_URL, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                "Event archive fetch failed",
                event="crawler_url_fetch_failed",
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = parse_events(response.text)
        for record in records:
            try:
                detail_response = session.get(record["url"], timeout=30)
                detail_response.raise_for_status()
                record["description"] = parse_detail_description(detail_response.text) or record["description"]
            except requests.RequestException as error:
                log_message(
                    "Event detail fetch failed",
                    event="crawler_url_fetch_failed",
                    url=record["url"],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message("Event archive parsed", event="crawler_parse_completed", record_count=len(records))
        return records


def main():
    NtsoOrgCrawler().run()


if __name__ == "__main__":
    main()
