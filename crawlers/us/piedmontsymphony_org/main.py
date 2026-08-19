import json
import html as html_lib
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Piedmont Symphony Orchestra"
SOURCE_URL = "https://www.piedmontsymphony.org/"
EVENTS_URL = urljoin(SOURCE_URL, "event-list")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "Accept-Language": "en-US,en;q=0.9",
}
CITY_STATE_RE = re.compile(r",\s*([^,]+),\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?\s*,\s*USA\s*$")


def _clean_text(value):
    if not value:
        return None
    text = html_lib.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _event_links(html):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for anchor in soup.select('a[href*="/event-details/"]'):
        url = urljoin(EVENTS_URL, anchor.get("href", ""))
        parsed = urlparse(url)
        if parsed.netloc in {"piedmontsymphony.org", "www.piedmontsymphony.org"}:
            links.add(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
    return sorted(links)


def _event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or "")
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "Event":
                return candidate
    return None


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _description(soup):
    section = soup.select_one('[data-hook="about-section"]')
    if not section:
        return None
    heading = section.select_one('[data-hook="about"]')
    if heading:
        heading.decompose()
    return _clean_text(section.get_text("\n", strip=True))


def _parse_event(html, url):
    soup = BeautifulSoup(html, "html.parser")
    schema = _event_schema(soup)
    if not schema:
        return None

    title = _clean_text(schema.get("name"))
    start = _parse_datetime(schema.get("startDate"))
    location = schema.get("location") if isinstance(schema.get("location"), dict) else {}
    venue = _clean_text(location.get("name"))
    address = location.get("address")
    if isinstance(address, dict):
        city = _clean_text(address.get("addressLocality"))
    else:
        match = CITY_STATE_RE.search(_clean_text(address) or "")
        city = _clean_text(match.group(1)) if match else None

    if not all((title, start, venue, city)):
        return None
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": start.strftime("%H:%M"),
        "venue": venue,
        "city": city,
        "description": _description(soup),
    }


class PiedmontSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="piedmontsymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        log_message("Fetching event listing", event="crawler_url_fetch", url=EVENTS_URL)
        response = session.get(EVENTS_URL, timeout=45)
        response.raise_for_status()

        records = []
        skipped = 0
        for url in _event_links(response.text):
            try:
                detail = session.get(url, timeout=45)
                detail.raise_for_status()
                record = _parse_event(detail.text, url)
            except requests.RequestException as error:
                log_message(
                    "Event detail request failed",
                    event="crawler_url_fetch_failed",
                    level="warning",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                skipped += 1

        if skipped:
            log_message(
                "Skipped event pages without a complete event schema",
                event="crawler_records_skipped",
                level="warning",
                url=EVENTS_URL,
                record_count=skipped,
            )
        log_message(
            "Event listing parsed",
            event="crawler_scrape_completed",
            url=EVENTS_URL,
            record_count=len(records),
        )
        return sorted(records, key=lambda item: (item["date"], item["time_from"], item["title"]))


def main():
    PiedmontSymphonyCrawler().run()


if __name__ == "__main__":
    main()
