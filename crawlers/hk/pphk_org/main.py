import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.pphk.org/"
SOURCE = "Premiere Performances Hong Kong"
SITEMAPS = (
    "https://www.pphk.org/event-sitemap.xml",
    "https://www.pphk.org/festival_event-sitemap.xml",
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)",
    "Accept-Language": "en-HK,en;q=0.9",
}
TIMEOUT = 45
MONTHS = {
    name.lower(): number for number, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"), 1
    )
}


def _clean_text(node):
    if node is None:
        return None
    text = node.get_text("\n", strip=True) if hasattr(node, "get_text") else str(node)
    text = re.sub(r"[ \t\r\f\v]+", " ", text.replace("\xa0", " "))
    text = re.sub(r" *\n *", "\n", text)
    return text.strip() or None


def _event_json(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data.get("@graph", []) if isinstance(data, dict) else []
        candidates += [data] if isinstance(data, dict) else data
        for item in candidates:
            event_type = item.get("@type") if isinstance(item, dict) else None
            if event_type == "Event" or isinstance(event_type, list) and "Event" in event_type:
                return item
    return None


def _parse_time(value):
    match = re.fullmatch(r"(\d{1,2})(?::([0-5]\d))?\s*([ap])m", value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "p" else 0)
    return f"{hour:02d}:{match.group(2) or '00'}"


def _occurrences(date_text, fallback_date, fallback_time):
    dates = []
    match = re.search(
        r"\b(\d{1,2})(?:\s*&\s*(\d{1,2}))?\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r",?\s+(20\d{2})\b",
        date_text or "", re.I,
    )
    if match:
        for day_text in (match.group(1), match.group(2)):
            if not day_text:
                continue
            try:
                dates.append(date(int(match.group(4)), MONTHS[match.group(3).lower()], int(day_text)).isoformat())
            except ValueError:
                pass
    if not dates and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", fallback_date or ""):
        try:
            dates = [date.fromisoformat(fallback_date).isoformat()]
        except ValueError:
            pass

    time_area = date_text[match.end():] if match else (date_text or "")
    times = []
    joined = re.search(r"\b(\d{1,2}(?::[0-5]\d)?)\s*&\s*(\d{1,2}(?::[0-5]\d)?)\s*([ap])m\b", time_area, re.I)
    if joined:
        times = [_parse_time(f"{joined.group(1)}{joined.group(3)}m"),
                 _parse_time(f"{joined.group(2)}{joined.group(3)}m")]
    else:
        times = [_parse_time("".join(item)) for item in re.findall(
            r"\b(\d{1,2})(:[0-5]\d)?\s*([ap]m)\b", time_area, re.I
        )]
    times = [value for value in times if value]
    if not times:
        parsed_fallback = _parse_time((fallback_time or "").replace(".", "").replace(" ", ""))
        times = [parsed_fallback] if parsed_fallback else [None]
    return [(event_date, event_time) for event_date in dates for event_time in dict.fromkeys(times)]


def _parse_page(url, html):
    soup = BeautifulSoup(html, "html.parser")
    event = _event_json(soup)
    if not event:
        return []

    title = unescape(_clean_text(event.get("name")) or "")
    location = event.get("location") or {}
    venue = _clean_text(location.get("name")) if isinstance(location, dict) else None
    address = location.get("address") or {} if isinstance(location, dict) else {}
    country = _clean_text(address.get("addressCountry")) if isinstance(address, dict) else None
    locality = _clean_text(address.get("addressLocality")) if isinstance(address, dict) else None
    if country and country.lower() not in {"hong kong", "hk"}:
        # A home-country default is unsafe for touring performances. Foreign
        # records also need a resolved ISO code, which these pages do not expose.
        return []
    city = locality or "Hong Kong"
    if not title or not venue:
        return []

    date_node = soup.select_one("span.big.date")
    occurrences = _occurrences(
        _clean_text(date_node), _clean_text(event.get("startDate")),
        _clean_text(event.get("doorTime")),
    )
    if not occurrences:
        return []

    # The first content column includes its nested programme block, preserving
    # composer/work detail without pulling in ticketing or artist biographies.
    description = _clean_text(soup.select_one(".event-content.c1")) or _clean_text(event.get("description"))

    return [{
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": event_time,
        "venue": venue,
        "city": city,
        "country_code": "HK",
        "description": description,
        "source_url": SOURCE_URL,
        "source": SOURCE,
    } for event_date, event_time in occurrences]


class PphkOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="pphk_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="HK",
        upload_target="potential",
        columns=[
            "title", "date", "url", "time_from", "venue", "city",
            "country_code", "description", "source_url", "source",
        ],
        dedupe_subset=["date", "time_from", "venue", "title"],
    )

    def _get(self, url):
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        return response.text

    def scrape(self):
        urls = []
        for sitemap_url in SITEMAPS:
            try:
                soup = BeautifulSoup(self._get(sitemap_url), "xml")
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch Premiere Performances sitemap",
                    event="crawler_fetch_failed", level="error", url=sitemap_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                raise
            urls.extend(loc.get_text(strip=True) for loc in soup.select("url > loc"))
        urls = list(dict.fromkeys(url for url in urls if "/zh-hant/" not in url))

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._get, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(_parse_page(url, future.result()))
                except requests.RequestException as error:
                    log_message(
                        "Failed to fetch Premiere Performances event",
                        event="crawler_event_fetch_failed", level="warning", url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        return records


def main():
    return PphkOrgCrawler().run()


if __name__ == "__main__":
    main()
