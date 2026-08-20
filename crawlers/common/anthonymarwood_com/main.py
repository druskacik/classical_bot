import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.anthonymarwood.com/"
CONCERTS_URL = f"{SOURCE_URL}concerts"
SOURCE = "Anthony Marwood"
TIMEOUT = 30

# The artist's calendar is international.  These are venues and locations as
# they are styled on the calendar; linked promoters' Event JSON-LD takes
# precedence when it is available.
KNOWN_LOCATIONS = {
    "segerstrom center for the arts": ("Segerstrom Center for the Arts", "Costa Mesa", "US"),
    "royal academy of music": ("Royal Academy of Music", "London", "GB"),
    "wigmore hall": ("Wigmore Hall", "London", "GB"),
    "ukaria": ("UKARIA Cultural Centre", "Mount Barker Summit", "AU"),
    "the neilson": ("The Neilson", "Sydney", "AU"),
}

COUNTRY_HINTS = {
    " usa": "US", " california": "US", " vermont": "US", " maine": "US",
    " canada": "CA", " australia": "AU", " uk": "GB", " london": "GB",
    " belfast": "GB", " poole": "GB", " bath": "GB", " cornwall": "GB",
    " sussex": "GB",
}


def _clean(value):
    if not value:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text(" ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip() or None


def _jsonld_events(soup):
    events = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("@type") == "Event":
                events.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for script in soup.select('script[type="application/ld+json"]'):
        payload = script.string or script.get_text()
        if not payload or len(payload) > 500_000:
            continue
        try:
            visit(json.loads(payload))
        except (json.JSONDecodeError, TypeError):
            continue
    return events


def _country_code(address, url, text):
    if isinstance(address, dict):
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        if country and re.fullmatch(r"[A-Za-z]{2}", str(country)):
            return str(country).upper()
        address = " ".join(str(v) for v in address.values() if v)
    haystack = f" {address or ''} {url} {text}".lower()
    if re.search(r",\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", str(address or "")):
        return "US"
    for hint, code in COUNTRY_HINTS.items():
        if hint in haystack:
            return code
    return None


def _city_from_address(address):
    if isinstance(address, dict):
        return _clean(address.get("addressLocality"))
    if not address:
        return None
    match = re.search(r"\n\s*([A-Za-z .'-]+),?\s+[A-Z]{2}\s+\d", str(address))
    return _clean(match.group(1)) if match else None


def _detail_records(url, wanted_dates, fallback_title, calendar_text):
    """Read portable schema.org Event data from a linked first-party promoter."""
    try:
        log_message("Fetching linked concert detail", event="crawler_url_fetch", url=url)
        response = requests.get(url, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            "Linked concert detail unavailable",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    records = []
    seen = set()
    for event in _jsonld_events(BeautifulSoup(response.text, "html.parser")):
        starts = event.get("startDate")
        if not starts:
            continue
        if not isinstance(starts, list):
            starts = [starts]
        for start in starts:
            try:
                parsed = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            except ValueError:
                continue
            event_date = parsed.date().isoformat()
            if event_date not in wanted_dates:
                continue
            location = event.get("location") or {}
            if isinstance(location, list):
                location = location[0] if location else {}
            if not isinstance(location, dict):
                continue
            address = location.get("address")
            venue = _clean(location.get("name"))
            city = _city_from_address(address)
            country = _country_code(address, url, calendar_text)
            key = (event_date, venue, city, parsed.time().isoformat(timespec="minutes"))
            if not venue or not city or not country or key in seen:
                continue
            seen.add(key)
            records.append({
                "title": _clean(event.get("name")) or fallback_title,
                "date": event_date,
                "url": url,
                "time_from": parsed.time().isoformat(timespec="minutes") if "T" in str(start) else None,
                "venue": venue,
                "city": city,
                "country_code": country,
                "description": _clean(event.get("description")) or calendar_text,
            })
    return records


def _calendar_entries(soup):
    year = None
    for paragraph in soup.select("p"):
        text = _clean(paragraph.get_text(" ", strip=True))
        if not text:
            continue
        if re.fullmatch(r"20\d{2}", text):
            year = int(text)
            continue
        match = re.match(r"^([A-Z][a-z]+)\s+([0-9, ]+):\s*(.+)$", text)
        if not match or year is None:
            continue
        month, day_list, title = match.groups()
        dates = []
        for day in re.findall(r"\d+", day_list):
            try:
                dates.append(datetime.strptime(f"{year} {month} {day}", "%Y %B %d").date().isoformat())
            except ValueError:
                pass
        if not dates:
            continue
        links = [a.get("href") for a in paragraph.select("a[href]") if a.get("href", "").startswith("http")]
        yield text, title.strip(), dates, links


def _inline_times(text):
    times = []
    for hour, minute, meridiem in re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.I):
        value = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
        times.append(f"{value:02d}:{int(minute or 0):02d}")
    return times or [None]


class AnthonyMarwoodCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="anthonymarwood_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "time_from", "venue", "title"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=CONCERTS_URL)
        response = requests.get(CONCERTS_URL, timeout=TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []
        entries = list(_calendar_entries(soup))

        def fetch_details(entry):
            calendar_text, title, dates, links = entry
            found = []
            for url in links:
                if re.search(r"/(?:events?|whats-on)/", url):
                    found.extend(_detail_records(url, set(dates), title, calendar_text))
            return found

        with ThreadPoolExecutor(max_workers=min(8, len(entries) or 1)) as executor:
            details = list(executor.map(fetch_details, entries))

        for (calendar_text, title, dates, links), detail_records in zip(entries, details):
            if detail_records:
                records.extend(detail_records)
                continue

            lowered = calendar_text.lower()
            location = next((value for key, value in KNOWN_LOCATIONS.items() if key in lowered), None)
            if not location:
                # A performer, city, region, or festival name is not a venue.
                continue
            venue, city, country = location
            for event_date in dates:
                for time_from in _inline_times(calendar_text):
                    records.append({
                        "title": title,
                        "date": event_date,
                        "url": links[0] if links else CONCERTS_URL,
                        "time_from": time_from,
                        "venue": venue,
                        "city": city,
                        "country_code": country,
                        "description": calendar_text,
                    })

        log_message("Concert calendar parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    AnthonyMarwoodCrawler().run()


if __name__ == "__main__":
    main()
