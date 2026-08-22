import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.riccardominasi.com/"
SCHEDULE_URL = urljoin(SOURCE_URL, "schedule")
SOURCE = "Riccardo Minasi"
SITE_TIMEZONE = ZoneInfo("Europe/Paris")

COUNTRY_CODES = {
    "argentina": "AR",
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "canada": "CA",
    "denmark": "DK",
    "france": "FR",
    "germany": "DE",
    "italy": "IT",
    "japan": "JP",
    "luxembourg": "LU",
    "mexico": "MX",
    "netherlands": "NL",
    "peru": "PE",
    "poland": "PL",
    "scotland": "GB",
    "spain": "ES",
    "switzerland": "CH",
    "uk": "GB",
    "united kingdom": "GB",
    "uruguay": "UY",
}


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def body_lines(html):
    text = BeautifulSoup(html or "", "html.parser").get_text("\n")
    return [clean_text(line) for line in text.splitlines() if clean_text(line)]


def labelled_value(lines, label):
    wanted = label.casefold()
    for index, line in enumerate(lines):
        if line.casefold().rstrip(":").strip() != wanted:
            continue
        for value in lines[index + 1 :]:
            if value != ":":
                return value
    return None


def parse_city_country(value):
    value = clean_text(value)
    if not value or value == ":":
        return None, None

    if "," in value:
        city, country = value.rsplit(",", 1)
    else:
        parts = value.rsplit(maxsplit=1)
        if len(parts) != 2:
            return None, None
        city, country = parts

    city = clean_text(city)
    country_code = COUNTRY_CODES.get(clean_text(country).casefold())
    return (city or None), country_code


def event_datetime(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, tz=SITE_TIMEZONE)


def event_title(raw_title):
    title = clean_text(raw_title)
    return re.sub(
        r"^\d{1,2}\s+[A-Za-z]+\s+\d{4}\s*[-–—,]\s*",
        "",
        title,
        count=1,
    ) or title


def parse_event(item):
    lines = body_lines(item.get("body"))
    venue = labelled_value(lines, "venue")
    location = labelled_value(lines, "city, country")
    city, country_code = parse_city_country(location)

    if not venue or not city or not country_code:
        log_message(
            "Skipping event with incomplete location",
            event="crawler_record_skipped",
            url=urljoin(SOURCE_URL, item.get("fullUrl", "")),
            error_type="IncompleteLocation",
        )
        return None

    start = event_datetime(item["startDate"])
    end = event_datetime(item["endDate"]) if item.get("endDate") else None
    return {
        "title": event_title(item.get("title")),
        "date": start.date().isoformat(),
        "url": urljoin(SOURCE_URL, item["fullUrl"]),
        "time_from": start.strftime("%H:%M"),
        "time_to": end.strftime("%H:%M") if end else None,
        "venue": clean_text(venue),
        "city": city,
        "country_code": country_code,
        "description": "\n".join(lines) or None,
    }


class RiccardoMinasiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="riccardominasi_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url"],
    )

    def scrape(self):
        records = []
        seen_ids = set()
        page_url = f"{SCHEDULE_URL}?format=json"

        while page_url:
            log_message("Fetching schedule page", event="crawler_url_fetch", url=page_url)
            response = requests.get(page_url, timeout=30)
            response.raise_for_status()
            payload = response.json()

            for item in payload.get("upcoming", []) + payload.get("past", []):
                item_id = item.get("id")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                record = parse_event(item)
                if record:
                    records.append(record)

            next_url = payload.get("pagination", {}).get("nextPageUrl")
            if next_url:
                separator = "&" if "?" in next_url else "?"
                page_url = urljoin(SOURCE_URL, next_url) + separator + "format=json"
            else:
                page_url = None

        return records


def main():
    RiccardoMinasiCrawler().run()


if __name__ == "__main__":
    main()
