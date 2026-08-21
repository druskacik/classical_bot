import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.kobekina.info/"
CONCERTS_URL = "https://www.kobekina.info/concerts"
SOURCE = "Anastasia Kobekina"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"}
COUNTRY_CODES = {
    "Australia": "AU",
    "Austria": "AT",
    "Belgium": "BE",
    "Finland": "FI",
    "France": "FR",
    "Germany": "DE",
    "Italy": "IT",
    "New Zealand": "NZ",
    "South Korea": "KR",
    "Switzerland": "CH",
    "The Netherlands": "NL",
}
DATE_LINE = re.compile(
    r"^(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.?\s+"
    r"(?P<city>[^,]+),\s*(?P<country>.+?)\s*$"
)
VENUE_WORDS = re.compile(
    r"\b(philharmoni(?:e|c)|philhamornie|concertgebouw|konzerthaus|musikverein|opera house|"
    r"stadttheater|concert hall|auditorium|theatre|theater|hall|church|cathedral|"
    r"basilica|abbey|castle|museum|centre|center)\b",
    re.IGNORECASE,
)
CITY_TIMEZONES = {
    "Sydney": "Australia/Sydney",
    "Seoul": "Asia/Seoul",
    "Auckland": "Pacific/Auckland",
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u200b", " ")).strip()


def _json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_objects(child)


def _event_jsonld(soup: BeautifulSoup, month: int, day: int) -> dict | None:
    events = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        objects = list(_json_objects(data))
        by_id = {item.get("@id"): item for item in objects if item.get("@id")}
        for item in objects:
            types = item.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if not any(str(kind).endswith("Event") for kind in types):
                continue
            start = item.get("startDate")
            try:
                parsed = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if (parsed.month, parsed.day) == (month, day):
                event = dict(item)
                location = event.get("location")
                if isinstance(location, dict) and location.get("@id") in by_id:
                    event["location"] = by_id[location["@id"]]
                events.append(event)
    return events[0] if events else None


def _venue_name(location: Any) -> str | None:
    if isinstance(location, list):
        for item in location:
            venue = _venue_name(item)
            if venue:
                return venue
    if isinstance(location, dict):
        name = _clean(str(location.get("name") or ""))
        if name:
            return name
    return None


def _parse_index(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    date_node = soup.find(string=lambda text: bool(text and DATE_LINE.match(_clean(text))))
    if not date_node:
        return []
    container = date_node.find_parent("div", class_="wixui-rich-text")
    if not container:
        return []

    entries = []
    current = None
    for node in container.find_all(["p", "h1", "h2", "h3"], recursive=True):
        text = _clean(node.get_text(" ", strip=True))
        match = DATE_LINE.match(text)
        if match:
            if current:
                entries.append(current)
            current = {**match.groupdict(), "details": [], "url": None}
        elif current and text.upper() == "TICKETS":
            link = node.find("a", href=True)
            if link:
                current["url"] = link["href"]
        elif current and text and text.upper() not in {
            "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
            "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
        }:
            current["details"].append(text)
    if current:
        entries.append(current)
    return [entry for entry in entries if entry["url"]]


def _enrich(entry: dict) -> dict | None:
    url = entry["url"]
    month, day = int(entry["month"]), int(entry["day"])
    event = None
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        event = _event_jsonld(BeautifulSoup(response.text, "html.parser"), month, day)
    except requests.RequestException as error:
        log_message(
            "Concert detail fetch failed",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    country = _clean(entry["country"])
    country_code = COUNTRY_CODES.get(country)
    if not country_code:
        log_message("Skipping unknown country", event="crawler_record_skipped", url=url)
        return None

    venue = _venue_name(event.get("location")) if event else None
    if not venue:
        venue = next((line for line in entry["details"] if VENUE_WORDS.search(line)), None)
    if not venue:
        log_message("Skipping event without venue", event="crawler_record_skipped", url=url)
        return None
    residency = re.match(r"^Residency (?:at|in) (?:the )?(.+)$", venue, re.IGNORECASE)
    if residency:
        venue = residency.group(1)
    venue = venue.replace("Philhamornie", "Philharmonie")

    start = None
    if event:
        try:
            start = datetime.fromisoformat(str(event.get("startDate")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass
    event_date = start.date() if start else date(date.today().year, month, day)
    if start and str(event.get("startDate", "")).endswith("Z"):
        timezone = CITY_TIMEZONES.get(_clean(entry["city"]))
        if timezone:
            start = start.astimezone(ZoneInfo(timezone))
    time_from = start.strftime("%H:%M") if start and "T" in str(event.get("startDate")) else None

    details = list(dict.fromkeys(entry["details"]))
    structured_description = _clean(str(event.get("description") or "")) if event else ""
    description = "\n".join(details)
    if structured_description and structured_description not in description:
        description = "\n".join(filter(None, [description, structured_description]))
    title_detail = next((line for line in reversed(details) if line != venue), None)
    title = _clean(str(event.get("name") or "")) if event else ""
    if not title:
        title = f"Anastasia Kobekina — {title_detail or 'Concert'}"

    return {
        "title": title,
        "date": event_date.isoformat(),
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": _clean(entry["city"]),
        "country_code": country_code,
        "description": description or None,
    }


class KobekinaInfoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="kobekina_info",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "url", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert index", event="crawler_url_fetch", url=CONCERTS_URL)
        response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=30)
        response.raise_for_status()
        entries = _parse_index(response.text)
        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(_enrich, entry) for entry in entries]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)
        records.sort(key=lambda record: (record["date"], record["city"], record["url"]))
        return records


def main():
    KobekinaInfoCrawler().run()


if __name__ == "__main__":
    main()
