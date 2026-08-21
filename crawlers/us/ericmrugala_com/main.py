import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Eric Mrugala"
SOURCE_URL = "https://www.ericmrugala.com/"
EVENTS_URL = urljoin(SOURCE_URL, "events")
CALENDAR_FEATURE_ID = "246316"
REQUEST_TIMEOUT = 30

# The calendar omits the city for this venue, but the venue's name itself
# unambiguously identifies Plymouth, Massachusetts.
VENUE_CITY_DEFAULTS = {
    "Plymouth Memorial Hall": "Plymouth",
}


def _clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def _canonical_event_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _location_parts(location):
    location = _clean_text(location)
    if not location:
        return None, None

    parts = [_clean_text(part) for part in location.split(",")]
    parts = [part for part in parts if part]
    venue = parts[0]

    # US calendar locations normally end in "city, ST". Detail pages may
    # insert a street address between the venue and city.
    city = None
    if len(parts) >= 3 and re.fullmatch(r"[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?", parts[-1]):
        city = parts[-2]
    elif venue in VENUE_CITY_DEFAULTS:
        city = VENUE_CITY_DEFAULTS[venue]

    return venue, city


def _json_ld_event(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") in {"Event", "MusicEvent"}:
                return item
    return {}


def _description(soup):
    notes = soup.select_one(".event-description > .event-notes")
    if not notes:
        return None
    paragraphs = [
        _clean_text(node.get_text(" ", strip=True))
        for node in notes.select("p, li")
    ]
    paragraphs = [text for text in paragraphs if text]
    return "\n\n".join(paragraphs) or _clean_text(notes.get_text(" ", strip=True))


def _parse_detail(session, detail_url):
    log_message("Fetching concert detail", event="crawler_url_fetch", url=detail_url)
    response = session.get(detail_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    data = _json_ld_event(soup)

    title_node = soup.select_one(".event-title")
    title = _clean_text(data.get("name")) or _clean_text(
        title_node.get_text(" ", strip=True) if title_node else None
    )
    start = data.get("startDate")
    end = data.get("endDate")
    if not start:
        date_node = soup.select_one(".date-long time.from .date")
        time_node = soup.select_one(".date-long time.from .time")
        if date_node:
            from datetime import datetime

            parsed_date = datetime.strptime(date_node.get_text(strip=True), "%A, %B %d, %Y")
            start = parsed_date.strftime("%Y-%m-%d")
            if time_node:
                start += "T" + datetime.strptime(
                    time_node.get_text(strip=True), "%I:%M%p"
                ).strftime("%H:%M:%S")

    location_data = data.get("location") if isinstance(data.get("location"), dict) else {}
    location_node = soup.select_one(".event-location span")
    location_text = _clean_text(
        ", ".join(
            value for value in (location_data.get("name"), location_data.get("address")) if value
        )
    ) or _clean_text(location_node.get_text(" ", strip=True) if location_node else None)
    venue, city = _location_parts(location_text)

    canonical = data.get("url")
    if not canonical and title_node and title_node.find("a", href=True):
        canonical = urljoin(SOURCE_URL, title_node.find("a", href=True)["href"])
    canonical = _canonical_event_url(canonical or detail_url)

    if not all((title, start, venue, city)):
        log_message(
            "Skipping event with incomplete required fields",
            event="crawler_record_skipped",
            url=canonical,
            missing_fields=[
                name
                for name, value in (("title", title), ("date", start), ("venue", venue), ("city", city))
                if not value
            ],
        )
        return None

    time_to = end.split("T", 1)[1][:5] if end and "T" in end else None
    if not time_to:
        end_node = soup.select_one(".date-long time.to .time")
        if end_node:
            from datetime import datetime

            time_to = datetime.strptime(end_node.get_text(strip=True), "%I:%M%p").strftime("%H:%M")

    return {
        "title": title,
        "date": start.split("T", 1)[0],
        "url": canonical,
        "time_from": start.split("T", 1)[1][:5] if "T" in start else None,
        "time_to": time_to,
        "venue": venue,
        "city": city,
        "country_code": "US",
        "description": _description(soup),
    }


def _event_links(soup):
    links = []
    seen = set()
    for anchor in soup.select('a.event_details[href*="/go/events/"]'):
        url = urljoin(SOURCE_URL, anchor.get("href"))
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def _calendar_pages(session):
    response = session.get(EVENTS_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    yield BeautifulSoup(response.text, "html.parser")

    page = 1
    while True:
        url = (
            f"{EVENTS_URL}/features/load/calendar_feature_{CALENDAR_FEATURE_ID}"
            f".turbo_stream?calendar_page_prev={page}"
        )
        log_message("Fetching previous events page", event="crawler_url_fetch", url=url)
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        if not _event_links(soup):
            break
        yield soup
        next_link = soup.select_one(
            f'a[href*="calendar_page_prev={page + 1}"]'
        )
        if not next_link:
            break
        page += 1


class EricMrugalaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="ericmrugala_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0; "
            "+https://github.com/)"
        )

        detail_urls = []
        seen = set()
        for page in _calendar_pages(session):
            for url in _event_links(page):
                if url not in seen:
                    seen.add(url)
                    detail_urls.append(url)

        records = []
        for url in detail_urls:
            try:
                record = _parse_detail(session, url)
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

        log_message(
            "Concert records parsed",
            event="crawler_parse_completed",
            record_count=len(records),
        )
        return records


def main():
    EricMrugalaCrawler().run()


if __name__ == "__main__":
    main()
