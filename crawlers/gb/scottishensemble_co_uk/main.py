import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://scottishensemble.co.uk/"
SOURCE = "Scottish Ensemble"
ARCHIVE_URL = urljoin(SOURCE_URL, "whats-on/programme-archive/")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*(am|pm)\b", re.I)

# Scottish Ensemble occasionally tours abroad. The archive is overwhelmingly UK
# based; these are the foreign locations present in or used by its programme.
FOREIGN_CITIES = {
    "Amsterdam": "NL",
    "Antwerp": "BE",
    "Berlin": "DE",
    "Brussels": "BE",
    "Cologne": "DE",
    "Dublin": "IE",
    "Hamburg": "DE",
    "Hong Kong": "HK",
    "New York": "US",
    "Paris": "FR",
    "Rüsselsheim": "DE",
    "Shanghai": "CN",
    "Sydney": "AU",
    "Toronto": "CA",
    "Vienna": "AT",
}


def _clean_text(element) -> str | None:
    if element is None:
        return None
    text = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
    return text or None


def _to_24_hour(value: str | None) -> str | None:
    if not value:
        return None
    match = TIME_RE.search(value)
    if not match:
        return None
    hours, minutes = map(int, match.group(1).split(":"))
    if match.group(2).lower() == "pm" and hours != 12:
        hours += 12
    elif match.group(2).lower() == "am" and hours == 12:
        hours = 0
    return f"{hours:02d}:{minutes:02d}"


def _country_code(city: str, event) -> str:
    if city in FOREIGN_CITIES:
        return FOREIGN_CITIES[city]
    address_button = event.select_one("button[data-bs-content]")
    address = address_button.get("data-bs-content", "") if address_button else ""
    address_markers = {
        "Germany": "DE",
        "Deutschland": "DE",
        "Ireland": "IE",
        "Netherlands": "NL",
        "Belgium": "BE",
        "France": "FR",
        "USA": "US",
        "United States": "US",
        "Australia": "AU",
        "Canada": "CA",
        "China": "CN",
        "Hong Kong": "HK",
    }
    for marker, code in address_markers.items():
        if marker.lower() in address.lower():
            return code
    return "GB"


def _resolved_city(city: str, event) -> str:
    """Replace a country-level location label with the city in its address."""
    if city not in {"Germany", "Ireland", "France", "Belgium", "Netherlands"}:
        return city
    address_button = event.select_one("button[data-bs-content]")
    if address_button is None:
        return city
    address = BeautifulSoup(
        address_button.get("data-bs-content", ""), "html.parser"
    ).get_text(" ", strip=True)
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) < 2:
        return city
    candidate = parts[-2]
    candidate = re.sub(r"^(?:[A-Z]{1,2}-)?\d{4,6}\s+", "", candidate)
    return candidate or city


class ScottishEnsembleCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="scottishensemble_co_uk",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GB",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)"
        )

    def _get_soup(self, url: str) -> BeautifulSoup:
        log_message("Fetching page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.content, "html.parser")

    def _programme_urls(self) -> list[str]:
        urls = []
        page_number = 1
        while True:
            url = ARCHIVE_URL if page_number == 1 else urljoin(
                ARCHIVE_URL, f"page/{page_number}/"
            )
            soup = self._get_soup(url)
            page_urls = [
                link["href"]
                for link in soup.select("article.programme a[href*='/programme/']")
            ]
            if not page_urls:
                break
            urls.extend(page_urls)
            next_link = soup.select_one("a[aria-label='Next Page']")
            if next_link is None:
                break
            page_number += 1
        return list(dict.fromkeys(urls))

    def _parse_programme(self, url: str) -> list[dict]:
        soup = self._get_soup(url)
        title = _clean_text(soup.select_one("main h1"))
        if not title:
            return []

        description_parts = []
        intro = soup.select_one("section.programme-intro")
        if intro:
            description_parts.append(intro.get_text("\n", strip=True))
        description = "\n".join(description_parts) or None

        records = []
        for event in soup.select("section.flexi_events li.list-group-item"):
            time_element = event.select_one("time[datetime]")
            if time_element is None:
                continue
            event_date = time_element.get("datetime", "")[:10]
            try:
                date.fromisoformat(event_date)
            except ValueError:
                continue

            location = event.select_one("p.d-inline-flex")
            location_parts = [
                _clean_text(span) for span in location.select(":scope > span")
            ] if location else []
            location_parts = [part for part in location_parts if part]
            if len(location_parts) < 2:
                continue
            city, venue = location_parts[0], location_parts[1]
            country_code = _country_code(city, event)
            city = _resolved_city(city, event)
            time_container = time_element.parent
            time_from = _to_24_hour(_clean_text(time_container))
            records.append({
                "title": title,
                "date": event_date,
                "url": url,
                "time_from": time_from,
                "time_to": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description,
            })
        return records

    def scrape(self) -> list[dict]:
        records = []
        for url in self._programme_urls():
            try:
                records.extend(self._parse_programme(url))
            except requests.RequestException as error:
                log_message(
                    "Unable to fetch programme",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message(
            "Scraped concert occurrences",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    ScottishEnsembleCrawler().run()


if __name__ == "__main__":
    main()
