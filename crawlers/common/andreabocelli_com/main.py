import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Andrea Bocelli"
SOURCE_URL = "https://www.andreabocelli.com/"
TOUR_URL = "https://www.andreabocelli.com/tickets/"

# The official tour is international and spells out the country in each event.
# Include common tour destinations so newly announced dates do not require a
# crawler change merely because the itinerary expands.
COUNTRY_CODES = {
    "argentina": "AR",
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "brazil": "BR",
    "bulgaria": "BG",
    "canada": "CA",
    "chile": "CL",
    "china": "CN",
    "colombia": "CO",
    "croatia": "HR",
    "czech republic": "CZ",
    "czechia": "CZ",
    "denmark": "DK",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "greece": "GR",
    "hungary": "HU",
    "ireland": "IE",
    "israel": "IL",
    "italy": "IT",
    "japan": "JP",
    "mexico": "MX",
    "monaco": "MC",
    "netherlands": "NL",
    "new zealand": "NZ",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "romania": "RO",
    "saudi arabia": "SA",
    "serbia": "RS",
    "singapore": "SG",
    "slovakia": "SK",
    "slovenia": "SI",
    "south africa": "ZA",
    "south korea": "KR",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "turkey": "TR",
    "uae": "AE",
    "united arab emirates": "AE",
    "uk": "GB",
    "united kingdom": "GB",
    "usa": "US",
    "united states": "US",
    "united states of america": "US",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/131.0 Safari/537.36"
            )
        }
    )
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(403, 429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def parse_event(item) -> dict | None:
    link = item.select_one("a.m-gig__link[href]")
    date_element = item.select_one(".m-gig__date")
    location_element = item.select_one(".m-gig__title")
    venue_element = item.select_one(".m-gig__location")
    detail_element = item.select_one(".m-gig-detail__description")
    if not all((link, date_element, location_element, venue_element)):
        return None

    url = link.get("href", "").strip()
    venue = clean_text(venue_element.get_text(" ", strip=True))
    location = clean_text(location_element.get_text(" ", strip=True))
    location_parts = [part.strip() for part in location.rsplit(",", 1)]
    if len(location_parts) != 2:
        return None
    city, country_name = location_parts
    country_code = COUNTRY_CODES.get(country_name.casefold())
    if not all((url, venue, city, country_code)):
        return None

    date_text = clean_text(date_element.get_text(" ", strip=True))
    date_match = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})", date_text)
    if not date_match:
        return None
    try:
        event_date = datetime.strptime(" ".join(date_match.groups()), "%b %d %Y").date()
    except ValueError:
        return None

    description = clean_text(detail_element.get_text("\n", strip=True)) if detail_element else None
    time_match = re.search(r"\b(\d{1,2}:\d{2})\s*(am|pm)\b", description or "", re.I)
    time_from = None
    if time_match:
        time_from = datetime.strptime(
            f"{time_match.group(1)} {time_match.group(2)}", "%I:%M %p"
        ).strftime("%H:%M")

    return {
        "title": SOURCE,
        "date": event_date.isoformat(),
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class AndreaBocelliCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="andreabocelli_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching tour archive", event="crawler_url_fetch", url=TOUR_URL)
        response = make_session().get(TOUR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for item in soup.select("li.m-gig"):
            record = parse_event(item)
            if record is None:
                log_message(
                    "Skipping tour entry with incomplete event data",
                    event="crawler_record_skipped",
                    url=TOUR_URL,
                )
                continue
            records.append(record)

        log_message(
            "Parsed tour archive",
            event="crawler_parse_completed",
            url=TOUR_URL,
            record_count=len(records),
        )
        return records


def main():
    AndreaBocelliCrawler().run()


if __name__ == "__main__":
    main()
