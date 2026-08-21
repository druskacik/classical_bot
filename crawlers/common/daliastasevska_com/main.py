import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Dalia Stasevska"
SOURCE_URL = "https://www.daliastasevska.com/schedule"
SITEMAP_URL = "https://www.daliastasevska.com/sitemap.xml"

COUNTRY_CODES = {
    "austria": "AT",
    "australia": "AU",
    "belgium": "BE",
    "canada": "CA",
    "denmark": "DK",
    "england": "GB",
    "estonia": "EE",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "italy": "IT",
    "japan": "JP",
    "latvia": "LV",
    "lithuania": "LT",
    "netherlands": "NL",
    "monaco": "MC",
    "norway": "NO",
    "poland": "PL",
    "scotland": "GB",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "uk": "GB",
    "united kingdom": "GB",
    "usa": "US",
    "united states": "US",
    "wales": "GB",
}
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}
VENUE_DEFAULTS = {
    ("philorch.ensembleartsphilly.org", "Philadelphia"): "Marian Anderson Hall",
    ("www.rbo.org.uk", "London"): "Royal Opera House",
    ("www.metopera.org", "New York"): "Metropolitan Opera House",
}


def _clean_text(element):
    if element is None:
        return None
    text = element.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text or None


def _parse_location(value):
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) < 2:
        return None

    city = ", ".join(parts[:-1])
    region = parts[-1]
    upper_region = region.upper()
    if upper_region in US_STATE_CODES:
        return city, "US"
    if upper_region == "FI":
        return city, "FI"
    country_code = COUNTRY_CODES.get(region.casefold())
    if country_code:
        return city, country_code
    return None


def _make_date(month, day, year):
    value = f"{month} {day}, {year}"
    for date_format in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unsupported date: {value}")


def _parse_dates(value):
    """Expand only dates which the schedule presents as concrete occurrences."""
    value = re.sub(r"\s+", " ", value).strip()
    match = re.fullmatch(r"([A-Za-z]+) (\d{1,2}), (\d{4})", value)
    if match:
        try:
            return [_make_date(*match.groups())]
        except ValueError:
            return []

    match = re.fullmatch(r"([A-Za-z]+) ([\d, &]+), (\d{4})", value)
    if match and "&" in match.group(2):
        month, days, year = match.groups()
        try:
            return [_make_date(month, day, year) for day in re.findall(r"\d{1,2}", days)]
        except ValueError:
            return []

    match = re.fullmatch(r"([A-Za-z]+) (\d{1,2})-(\d{1,2}), (\d{4})", value)
    if match:
        month, first_day, last_day, year = match.groups()
        first_day, last_day = int(first_day), int(last_day)
        if last_day >= first_day and last_day - first_day <= 7:
            try:
                return [_make_date(month, str(day), year) for day in range(first_day, last_day + 1)]
            except ValueError:
                return []

    # A multi-month production span is not evidence of a performance every day.
    return []


def _scrape_archive_event(url):
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        box = soup.select_one(".schedule-cms1_title")
        if box is None:
            return []

        title = _clean_text(box.select_one("h1"))
        fields = box.select(".small-text")
        date_labels = [_clean_text(field) for field in fields[:2] if _clean_text(field)]
        venue = _clean_text(fields[2]) if len(fields) > 2 else None
        location = _clean_text(fields[3]) if len(fields) > 3 else None
        parsed_location = _parse_location(location or "")
        dates = [date for label in date_labels for date in _parse_dates(label)]

        if title and title.casefold() == "private concert":
            return []
        if not (title and venue and parsed_location and dates):
            return []

        city, country_code = parsed_location
        return [{
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": None,
        } for event_date in dates]
    except requests.RequestException as error:
        log_message(
            "Archive event fetch failed",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []


class DaliaStasevskaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="daliastasevska_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching schedule", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        for item in soup.select(".schedule-list3_item"):
            location = _clean_text(item.select_one(".small-text"))
            parsed_location = _parse_location(location or "")
            title = _clean_text(item.select_one(".schedule-list3_info-wrapper h4"))
            venue = _clean_text(item.select_one(".schedule-list3_info-wrapper h6"))
            link = item.select_one("a[href]")
            url = link.get("href", "").strip() if link else ""
            venue_default = None
            if parsed_location and url:
                city, _ = parsed_location
                venue_default = VENUE_DEFAULTS.get((urlparse(url).netloc.casefold(), city))
                venue = venue_default or venue
            date_labels = [
                _clean_text(date_element)
                for date_element in item.select("h4.h4-date")
                if _clean_text(date_element)
            ]
            dates = [date for label in date_labels for date in _parse_dates(label)]

            if not (parsed_location and title and venue and url and dates):
                log_message(
                    "Skipping incomplete schedule item",
                    event="crawler_record_skipped",
                    url=url or SOURCE_URL,
                    has_location=bool(parsed_location),
                    has_title=bool(title),
                    has_venue=bool(venue),
                    has_dates=bool(dates),
                )
                continue

            city, country_code = parsed_location
            description = _clean_text(item.select_one(".w-richtext"))
            if venue_default:
                work_title = _clean_text(item.select_one(".schedule-list3_info-wrapper h6"))
                description = "\n".join(part for part in (work_title, description) if part) or None
            for event_date in dates:
                records.append({
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                })

        log_message("Fetching event archive", event="crawler_url_fetch", url=SITEMAP_URL)
        sitemap_response = requests.get(SITEMAP_URL, timeout=30)
        sitemap_response.raise_for_status()
        sitemap = BeautifulSoup(sitemap_response.text, "xml")
        archive_urls = [
            location.get_text(strip=True)
            for location in sitemap.select("url loc")
            if "/event/" in location.get_text(strip=True)
        ]
        with ThreadPoolExecutor(max_workers=8) as executor:
            for archive_records in executor.map(_scrape_archive_event, archive_urls):
                records.extend(archive_records)

        log_message(
            "Schedule parsed",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    DaliaStasevskaCrawler().run()


if __name__ == "__main__":
    main()
