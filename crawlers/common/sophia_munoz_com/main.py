import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SITE_URL = "https://www.sophia-munoz.com/"
SOURCE_URL = urljoin(SITE_URL, "schedule")
SOURCE = "Sophia Muñoz"
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "MARCH": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUNE": 6, "JUL": 7, "JULY": 7,
    "AUG": 8, "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
COUNTRIES = {
    "UK": "GB", "UNITED KINGDOM": "GB", "POLAND": "PL",
    "LATVIA": "LV", "USA": "US", "UNITED STATES": "US",
    "GERMANY": "DE",
}


def clean(value):
    return re.sub(r"\s+", " ", value.replace("\u200b", " ").replace("\xa0", " ")).strip()


def iter_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json(child)


def event_metadata(soup):
    result = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for item in iter_json(data):
            kind = item.get("@type", "")
            kinds = kind if isinstance(kind, list) else [kind]
            if not any(str(value).endswith("Event") for value in kinds):
                continue
            location = item.get("location")
            if isinstance(location, dict):
                result["venue"] = clean(str(location.get("name", ""))) or None
            description = item.get("description")
            if description:
                result["description"] = clean(BeautifulSoup(str(description), "html.parser").get_text(" "))
            break
    return result


def fetch_detail(url):
    if not url:
        return {}
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            "Concert detail fetch failed", event="crawler_url_fetch_failed", url=url,
            error_type=type(error).__name__, error_message=str(error),
        )
        return {}
    soup = BeautifulSoup(response.text, "html.parser")
    metadata = event_metadata(soup)
    page_text = clean(soup.get_text(" "))
    host = urlparse(response.url).netloc.lower()
    if not metadata.get("venue") and "eif.co.uk" in host and re.search(r"The Queen['’]s Hall", page_text, re.I):
        metadata["venue"] = "The Queen's Hall"
    if not metadata.get("venue") and "hanzasperons.lv" in host:
        metadata["venue"] = "Hanzas Perons"
    description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
    if not metadata.get("description") and description_tag:
        metadata["description"] = clean(description_tag.get("content", "")) or None
    metadata["url"] = response.url
    return metadata


def parse_location(line):
    parts = [clean(part) for part in line.split(",")]
    country = COUNTRIES.get(parts[-1].upper()) if parts else None
    if not country or len(parts) < 2:
        return None, None
    return parts[0].title(), country


def parse_dates(lines, year):
    show_line = next((line for line in lines if line.upper().startswith("SHOWS ")), None)
    if show_line:
        tokens = re.findall(r"([A-Z]+\.?)[ ]*(\d{1,2}(?:\s*,\s*\d{1,2})*)", show_line.upper())
        dates = []
        for month_name, days in tokens:
            month = MONTHS.get(month_name.rstrip("."))
            if month:
                dates.extend(datetime(year, month, int(day)).date().isoformat() for day in re.findall(r"\d+", days))
        return dates
    match = re.fullmatch(r"([A-Z]+)\.?\s+(\d{1,2})", lines[0].upper())
    if not match or match.group(1) not in MONTHS:
        return []
    return [datetime(year, MONTHS[match.group(1)], int(match.group(2))).date().isoformat()]


def parse_time(lines):
    for line in lines:
        match = re.search(r"(?:APPROX\.\s*)?(\d{1,2})(?::(\d{2}))?\s*(AM|PM)$", line, re.I)
        if match:
            hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == "PM" else 0)
            return f"{hour:02d}:{int(match.group(2) or 0):02d}"
    return None


def parse_item(item):
    lines = [clean(line) for line in item.get_text("\n").splitlines()]
    lines = [line for line in lines if line and line.upper() != "TICKETS"]
    year_index = next((i for i, line in enumerate(lines) if re.fullmatch(r"20\d{2}", line)), None)
    if year_index is None or year_index == 0:
        return []
    year = int(lines[year_index])
    location_index = next((i for i in range(year_index + 1, len(lines)) if parse_location(lines[i])[1]), None)
    if location_index is None:
        return []
    city, country_code = parse_location(lines[location_index])
    link = item.select_one('a[href]:not([href=""])')
    detail_url = urljoin(SOURCE_URL, link["href"]) if link else None
    detail = fetch_detail(detail_url)
    venue = detail.get("venue")
    if not venue:
        log_message("Skipping event without a defensible venue", event="crawler_record_skipped", url=detail_url or SOURCE_URL)
        return []
    content = lines[year_index + 1:location_index]
    title = " — ".join(content)
    dates = parse_dates(lines, year)
    return [{
        "title": title,
        "date": date,
        "url": detail.get("url") or detail_url or SOURCE_URL,
        "time_from": parse_time(lines),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        # The artist's schedule itself provides billing/repertoire but no long-form
        # detail. Linked pages are used for venues only because archived links can
        # later be repurposed to a different performance.
        "description": "\n".join(content) or None,
    } for date in dates]


class SophiaMunozCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sophia_munoz_com",
        source=SOURCE,
        source_url=SITE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SITE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching schedule", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []
        for item in soup.select(".wixui-repeater__item"):
            records.extend(parse_item(item))
        log_message("Schedule parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    SophiaMunozCrawler().run()


if __name__ == "__main__":
    main()
