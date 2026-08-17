import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://peoriasymphony.org/"
SOURCE = "Peoria Symphony Orchestra"
SITEMAP_URL = urljoin(SOURCE_URL, "event-sitemap.xml")
DETAIL_WORKERS = 6
REQUEST_TIMEOUT = 30


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "ClassicalBot/1.0 (+concert calendar crawler)"})
    return session


def _get_soup(
    session: requests.Session,
    url: str,
    params: dict | None = None,
    parser: str = "html.parser",
) -> BeautifulSoup:
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.content, parser)


def _event_urls() -> list[str]:
    try:
        soup = _get_soup(_session(), SITEMAP_URL, parser="xml")
    except requests.RequestException as error:
        log_message(
            "Event sitemap fetch failed",
            event="crawler_url_fetch_failed",
            url=SITEMAP_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []
    return sorted(
        {
            loc.get_text(strip=True)
            for loc in soup.find_all("loc")
            if "/events/" in loc.get_text(strip=True)
        }
    )


def _parse_time(text: str) -> str | None:
    # The labelled time panel distinguishes the public start from lobby time.
    match = re.search(r"(\d{1,2}(?::\d{2})?\s*[ap]m)\s*start\b", text, re.IGNORECASE)
    if not match:
        time_range = re.search(
            r"\b(\d{1,2}(?::\d{2})?)\s*(?:-|–|to)\s*\d{1,2}(?::\d{2})?\s*([ap])m\b",
            text,
            re.IGNORECASE,
        )
        if time_range:
            value = f"{time_range.group(1)} {time_range.group(2)}m"
        else:
            matches = re.findall(r"\b\d{1,2}(?::\d{2})?\s*[ap]m\b", text, re.IGNORECASE)
            if not matches:
                return None
            value = matches[-1]
    else:
        value = match.group(1)

    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap])m", value.strip(), re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "p":
        hour += 12
    return f"{hour:02d}:{int(match.group(2) or 0):02d}"


def _event_details(url: str) -> list[dict]:
    session = _session()
    try:
        soup = _get_soup(session, url)
        ical_response = session.get(urljoin(url, "ical/"), timeout=REQUEST_TIMEOUT)
        ical_response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            "Event detail fetch failed",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    event = soup.select_one(".inside.event")
    location = event.select_one(".location.info-area .info") if event else None
    time_panel = event.select_one(".time.info-area .info") if event else None
    description = soup.select_one(".em-event-single")
    if not location:
        return []

    location_lines = list(location.stripped_strings)
    if len(location_lines) < 2:
        return []
    venue = location_lines[0].strip()
    city = None
    for line in reversed(location_lines[1:]):
        match = re.match(r"\s*([^,]+),\s*(?:IL|Illinois)\b", line, re.IGNORECASE)
        if match:
            city = match.group(1).strip()
            break
    # All published PSO locations observed are in Peoria; the organization's
    # home-city default is used only when the detail page gives a venue but
    # omits the locality (not when it names a different locality).
    if city is None and not any("," in line for line in location_lines[1:]):
        city = "Peoria"
    if not venue or not city:
        return []

    page_title = soup.find("h1")
    title = page_title.get_text(" ", strip=True) if page_title else ""
    if not title and soup.title:
        title = re.sub(r"\s+-\s+Peoria Symphony Orchestra\s*$", "", soup.title.get_text(strip=True))
    if not title:
        return []

    detail_text = description.get_text("\n", strip=True) if description else ""
    date_matches = re.findall(
        r"\b(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+)?"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2}),\s+(\d{4})\b",
        detail_text,
        re.IGNORECASE,
    )
    dates = []
    for month, day, year in date_matches:
        value = datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date().isoformat()
        if value not in dates:
            dates.append(value)
    ical = re.sub(r"\r?\n[ \t]", "", ical_response.text)
    start_match = re.search(r"^DTSTART[^:]*:(\d{8})(?:T(\d{4,6}))?", ical, re.MULTILINE)
    end_match = re.search(r"^DTEND[^:]*:(\d{8})(?:T(\d{4,6}))?", ical, re.MULTILINE)
    if not start_match:
        return []
    start_date = datetime.strptime(start_match.group(1), "%Y%m%d").date().isoformat()
    end_date = (
        datetime.strptime(end_match.group(1), "%Y%m%d").date().isoformat()
        if end_match
        else start_date
    )
    if start_date == end_date or not dates:
        dates = [start_date]

    start_time = None
    if start_match.group(2):
        digits = start_match.group(2)
        start_time = f"{digits[:2]}:{digits[2:4]}"

    common = {
        "title": title,
        "url": url,
        "time_from": start_time or (_parse_time(time_panel.get_text(" ", strip=True)) if time_panel else None),
        "venue": venue,
        "city": city,
        "description": detail_text or None,
    }
    return [{"date": event_date, **common} for event_date in dates]


class PeoriaSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="peoriasymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        event_urls = _event_urls()
        records = []
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as executor:
            futures = [executor.submit(_event_details, url) for url in event_urls]
            for future in as_completed(futures):
                records.extend(future.result())
        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["title"]))
        log_message(
            "Peoria Symphony calendar scraped",
            event="crawler_scrape_summary",
            record_count=len(records),
            event_page_count=len(event_urls),
        )
        return records


def main():
    PeoriaSymphonyCrawler().run()


if __name__ == "__main__":
    main()
