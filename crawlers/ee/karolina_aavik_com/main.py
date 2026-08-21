import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Karolina Aavik"
SOURCE_URL = "https://www.karolina-aavik.com/"
SITEMAP_URL = f"{SOURCE_URL}event-pages-sitemap.xml"
REQUEST_TIMEOUT = 30
MAX_WORKERS = 8
COUNTRY_CODES = {
    "austria": "AT",
    "belgium": "BE",
    "czechia": "CZ",
    "czech republic": "CZ",
    "denmark": "DK",
    "estonia": "EE",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "italy": "IT",
    "latvia": "LV",
    "lithuania": "LT",
    "malta": "MT",
    "netherlands": "NL",
    "norway": "NO",
    "poland": "PL",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "united kingdom": "GB",
}


def _clean_text(value):
    if not value:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text or None


def _fetch(url):
    log_message("Fetching crawler URL", event="crawler_url_fetch", url=url)
    response = requests.get(
        url,
        headers={"User-Agent": "classical-bot/1.0"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response


def _event_urls():
    soup = BeautifulSoup(_fetch(SITEMAP_URL).content, "xml")
    urls = []
    for location in soup.find_all("loc"):
        url = location.get_text(strip=True)
        if "/event-details/" in url:
            # Some Wix sitemap entries point at the booking form for an event.
            urls.append(url.removesuffix("/form"))
    return list(dict.fromkeys(urls))


def _find_event(data):
    app_data = data.get("appsWarmupData", {})
    for components in app_data.values():
        for component in components.values():
            if isinstance(component, dict) and isinstance(component.get("event"), dict):
                event = component["event"]
                return event.get("event", event)
    return None


def _location_fields(event):
    location = event.get("location") or {}
    full_address = location.get("fullAddress") or {}
    city = _clean_text(full_address.get("city"))
    country_code = full_address.get("country")
    if city and isinstance(country_code, str):
        return city, country_code.upper()

    # Older Wix events predate structured address fields. Their address is
    # still consistently comma-separated as street, postal-code city, country.
    address = _clean_text(location.get("address"))
    if not address:
        return None, None
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) < 2:
        return None, None
    country_code = COUNTRY_CODES.get(parts[-1].casefold())
    city_part = parts[-2]
    city = re.sub(r"^\s*[A-Z]{0,2}-?\d[\d -]*\s+", "", city_part, flags=re.I).strip()
    return (_clean_text(city), country_code)


def _venue(event, city):
    location = event.get("location") or {}
    name = _clean_text(location.get("name"))
    if name and name.casefold() != city.casefold():
        return name

    # Wix sometimes stores only the city as the location name while the title
    # names the actual venue in parentheses (for example Ukuaru muusikamaja).
    title = event.get("title") or ""
    match = re.search(r"\(([^()]*(?:hall|house|church|museum|centre|center|maja)[^()]*)\)", title, re.I)
    return _clean_text(match.group(1)) if match else None


def _local_datetime(value, timezone_name):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return instant.astimezone(ZoneInfo(timezone_name))


def _parse_event(url):
    response = _fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    warmup = soup.find("script", id="wix-warmup-data")
    if not warmup or not warmup.string:
        return None

    event = _find_event(json.loads(warmup.string))
    if not event:
        return None

    city, country_code = _location_fields(event)
    title = _clean_text(event.get("title"))
    scheduling = (event.get("scheduling") or {}).get("config") or {}
    start_value = scheduling.get("startDate")
    timezone_name = scheduling.get("timeZoneId")
    venue = _venue(event, city) if city else None

    if not all((title, city, venue, start_value, timezone_name)):
        return None
    if not isinstance(country_code, str) or not re.fullmatch(r"[A-Za-z]{2}", country_code):
        return None

    start = _local_datetime(start_value, timezone_name)
    end_value = scheduling.get("endDate")
    end = _local_datetime(end_value, timezone_name) if end_value else None
    description_parts = filter(None, (_clean_text(event.get("description")), _clean_text(event.get("about"))))
    slug = event.get("slug")
    canonical_url = f"{SOURCE_URL}event-details/{slug}" if slug else response.url.removesuffix("/form")

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": canonical_url,
        "time_from": start.strftime("%H:%M"),
        "time_to": None if scheduling.get("endDateHidden") or not end else end.strftime("%H:%M"),
        "venue": venue,
        "city": city,
        "country_code": country_code.upper(),
        "description": "\n".join(description_parts) or None,
    }


class KarolinaAavikCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="karolina_aavik_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="EE",
        upload_target="classical",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        urls = _event_urls()
        records = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_parse_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except Exception as error:
                    log_message(
                        "Failed to parse event detail",
                        event="crawler_url_error",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        records.sort(key=lambda record: (record["date"], record["time_from"], record["url"]))
        log_message(
            "Parsed event archive",
            event="crawler_archive_parsed",
            url=SITEMAP_URL,
            url_count=len(urls),
            record_count=len(records),
        )
        return records


def main():
    KarolinaAavikCrawler().run()


if __name__ == "__main__":
    main()
