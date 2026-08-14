import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Orchestre Philharmonique du Maroc"
SOURCE_URL = "https://www.opm.ma/"
SITEMAP_URL = "https://www.opm.ma/event-pages-sitemap.xml"
TIMEOUT = 30
MOROCCAN_CITIES = (
    "Casablanca",
    "Rabat",
    "Tanger",
    "Fès",
    "Marrakech",
    "Agadir",
    "Meknès",
    "Tétouan",
    "El Jadida",
    "Essaouira",
)


def _clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def _city_from_location(name, address):
    haystack = unicodedata.normalize("NFKD", f"{name or ''} {address or ''}").encode(
        "ascii", "ignore"
    ).decode().lower()
    for city in MOROCCAN_CITIES:
        normalized = unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode().lower()
        if re.search(rf"\b{re.escape(normalized)}\b", haystack):
            return city
    return None


def _event_urls(session):
    response = session.get(SITEMAP_URL, timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "xml")
    return [loc.get_text(strip=True) for loc in soup.find_all("loc") if "/event-details/" in loc.get_text()]


def _parse_event(session, url):
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    event = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        event = next((item for item in candidates if item.get("@type") == "Event"), event)
    if not event:
        return None

    title = _clean_text(event.get("name"))
    # Wix also indexes separately sold transport and ticket-category pages. The
    # former are not performances; the latter are collapsed below by occurrence.
    if not title or re.match(r"^OFFRE\s+BUS\b", title, re.IGNORECASE):
        return None

    try:
        start = datetime.fromisoformat(event["startDate"])
    except (KeyError, TypeError, ValueError):
        return None

    location = event.get("location") or {}
    venue = _clean_text(location.get("name"))
    address = location.get("address")
    if isinstance(address, dict):
        address = " ".join(str(value) for value in address.values() if value)
    city = _city_from_location(venue, address)
    if not venue or not city:
        return None
    if unicodedata.normalize("NFKD", venue).encode("ascii", "ignore").decode().lower() == (
        unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode().lower()
    ):
        return None

    about = soup.select_one('[data-hook="about-section"]')
    description = None
    if about:
        heading = about.select_one('[data-hook="about"]')
        if heading:
            heading.extract()
        description = _clean_text(about.get_text("\n", strip=True))

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": start.strftime("%H:%M:%S"),
        "venue": venue,
        "city": city,
        "description": description,
    }


def _occurrence_key(record):
    title = unicodedata.normalize("NFKD", record["title"]).encode("ascii", "ignore").decode().lower()
    title = re.sub(r"[/\s-]*(etudiant|adherent|tarif[^-]*)\b", "", title)
    title = re.sub(r"\s+", " ", title).strip(" -/")
    venue = unicodedata.normalize("NFKD", record["venue"]).encode("ascii", "ignore").decode().lower()
    return record["date"], record["time_from"], venue, title


class OpmCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="opm_ma",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="MA",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "time_from", "venue", "title"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"})
        urls = _event_urls(session)
        log_message("Fetching OPM event archive", event="crawler_archive_fetch", record_count=len(urls))

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_parse_event, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except Exception as error:
                    log_message(
                        "Unable to fetch OPM event",
                        event="crawler_url_fetch_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        unique = {}
        for record in sorted(records, key=lambda item: (item["date"], item["time_from"], item["title"])):
            key = _occurrence_key(record)
            previous = unique.get(key)
            if previous is None or len(record.get("description") or "") > len(previous.get("description") or ""):
                unique[key] = record
        return list(unique.values())


def main():
    OpmCrawler().run()


if __name__ == "__main__":
    main()
