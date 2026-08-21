from datetime import datetime
import re
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.fatmasaid.com/"
SOURCE = "Fatma Said"
COLLECTION_URL = urljoin(SOURCE_URL, "schedule-input?format=json")
SITE_TIMEZONE = ZoneInfo("Europe/Berlin")

COUNTRY_CODES = {
    "Austria": "AT",
    "Bahrain": "BH",
    "Belgium": "BE",
    "Bulgaria": "BG",
    "China": "CN",
    "Czech Republic": "CZ",
    "Denmark": "DK",
    "Egypt": "EG",
    "France": "FR",
    "GER": "DE",
    "Germany": "DE",
    "Holland": "NL",
    "Hungary": "HU",
    "Italy": "IT",
    "Luxembourg": "LU",
    "Monaco": "MC",
    "Netherlands": "NL",
    "Oman": "OM",
    "Scotland": "GB",
    "Spain": "ES",
    "Swizerland": "CH",
    "Switzerland": "CH",
    "The Netherlands": "NL",
    "Turkey": "TR",
    "UAE": "AE",
    "UK": "GB",
    "US": "US",
    "USA": "US",
    "United Kingdom": "GB",
}

CITY_CORRECTIONS = {
    "Kopenhagen": "Copenhagen",
    "Muscan": "Muscat",
    "Sardinia": "Cagliari",
    "Tuscon": "Tucson",
}

# These suffixes name a broadcast or festival, not a physical performance venue.
NON_VENUES = {"Global Citizen LIVE", "Morellino Classica Festival", "Liedrezital Zürich"}


def _text(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for link in soup.find_all("a"):
        if "ticket" in link.get_text(" ", strip=True).lower():
            link.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip() or None


def _parse_location(value):
    match = re.match(r"^\s*(.+?),\s*([^-]+?)\s+-\s+(.+?)\s*$", value or "")
    if not match:
        return None
    city, country, venue = (part.strip() for part in match.groups())
    if country == "MA":
        country = "US"
    country_code = COUNTRY_CODES.get(country)
    city = CITY_CORRECTIONS.get(city, city)
    venue = venue.strip()
    if not country_code or not city or not venue or venue in NON_VENUES:
        return None
    if city == "Cairo" and venue == "by the Pyramids":
        venue = "Pyramids of Giza"
    if venue == "BR Klassik Studio Concert":
        venue = "BR Klassik Studio"
    return city, country_code, venue


def _title(excerpt):
    soup = BeautifulSoup(excerpt or "", "html.parser")
    for candidate in soup.find_all(["strong", "b"]):
        text = candidate.get_text(" ", strip=True)
        if text and "ticket" not in text.lower() and "detail" not in text.lower():
            return text
    return "Fatma Said concert"


class FatmaSaidCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="fatmasaid_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        seen_ids = set()
        url = COLLECTION_URL

        while url:
            log_message("Fetching schedule page", event="crawler_url_fetch", url=url)
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    "Schedule page fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = [*(payload.get("upcoming") or []), *(payload.get("past") or [])]
            for event in events:
                if event.get("id") in seen_ids:
                    continue
                seen_ids.add(event.get("id"))
                location = _parse_location(event.get("title"))
                start_ms = event.get("startDate")
                if not location or not isinstance(start_ms, (int, float)):
                    continue
                city, country_code, venue = location
                start = datetime.fromtimestamp(start_ms / 1000, SITE_TIMEZONE)
                records.append(
                    {
                        "title": _title(event.get("excerpt")),
                        "date": start.date().isoformat(),
                        "url": urljoin(SOURCE_URL, event.get("fullUrl") or ""),
                        "time_from": start.strftime("%H:%M"),
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": _text(event.get("excerpt") or event.get("body")),
                    }
                )

            next_url = (payload.get("pagination") or {}).get("nextPageUrl")
            if next_url:
                separator = "&" if "?" in next_url else "?"
                url = urljoin(SOURCE_URL, f"{next_url}{separator}format=json")
            else:
                url = None

        return records


def main():
    FatmaSaidCrawler().run()


if __name__ == "__main__":
    main()
