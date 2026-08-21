import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Gävle Symfoniorkester"
SOURCE_URL = "https://www.gavlesymfoniorkester.se/"
LISTING_URL = "https://www.gavlekonserthus.se/Static/ConsertListListing.aspx"
DETAIL_BASE_URL = "https://www.gavlekonserthus.se/"
CATEGORY = "Symfoni"
PAGE_SIZE = 100

MONTHS = {
    "januari": 1,
    "februari": 2,
    "mars": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "augusti": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}

# The orchestra's own hall is in Gävle. The other entries are touring venues
# currently returned by the first-party Symfoni feed.
VENUE_CITIES = {
    "gevaliasalen": "Gävle",
    "gävle konserthus": "Gävle",
    "missionskyrkan kilafors": "Kilafors",
    "ockelbo kyrka": "Ockelbo",
    "söderhamns teater": "Söderhamn",
    "slottegymnasiet": "Ljusdal",
}


def _clean_text(element) -> str:
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def _session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({"User-Agent": "classical-concert-crawler/1.0"})
    return session


def _listing_payload(year: int, skip: int) -> dict:
    today = date.today()
    from_month = today.month if year == today.year else 1
    from_day = today.day if year == today.year else 1
    return {
        "sortOrder": "datum",
        "skipNumber": skip,
        "loadNumber": PAGE_SIZE,
        "category": CATEGORY,
        "dateFromYear": year,
        "dateFromMonth": from_month,
        "dateFromDate": from_day,
        "dateToYear": year,
        "dateToMonth": 12,
        "dateToDate": 31,
    }


def _detail_urls(session: requests.Session) -> list[tuple[str, int]]:
    urls = []
    seen = set()
    # The endpoint silently omits events when asked for a very wide date span,
    # so query one calendar year at a time. This also lets us check its archive.
    for year in range(2000, date.today().year + 11):
        skip = 0
        while True:
            log_message(
                "Fetching concert listing",
                event="crawler_url_fetch",
                url=LISTING_URL,
                category=CATEGORY,
                year=year,
                skip_number=skip,
            )
            response = session.post(
                LISTING_URL, data=_listing_payload(year, skip), timeout=30
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            cards = soup.select(".post-box")

            for card in cards:
                link = card.select_one("h2 a[href], h3 a[href]")
                if link is None:
                    continue
                url = urljoin(DETAIL_BASE_URL, link["href"])
                if url not in seen:
                    seen.add(url)
                    urls.append((url, year))

            if len(cards) < PAGE_SIZE:
                break
            skip += PAGE_SIZE

    return urls


def _label_value(soup: BeautifulSoup, label: str) -> str | None:
    for heading in soup.find_all(["h2", "h3"]):
        if label.casefold() not in _clean_text(heading).casefold():
            continue
        value = heading.find_next_sibling("p")
        if value is not None:
            text = _clean_text(value)
            return text or None
    return None


def _parse_datetime(value: str, fallback_year: int) -> tuple[str, str]:
    match = re.search(
        r"(\d{1,2})\s+([a-zåäö]+)(?:\s+(\d{4}))?\s*,?\s*(\d{1,2})[:.]([0-5]\d)",
        value.casefold(),
    )
    if match is None:
        raise ValueError(f"Unrecognized event date and time: {value!r}")

    day, month_name, year, hour, minute = match.groups()
    month = MONTHS.get(month_name)
    if month is None:
        raise ValueError(f"Unrecognized Swedish month: {month_name!r}")
    parsed = datetime(int(year or fallback_year), month, int(day), int(hour), int(minute))
    return parsed.date().isoformat(), parsed.time().strftime("%H:%M")


def _city_for(venue: str) -> str | None:
    normalized = venue.casefold().strip()
    if normalized in VENUE_CITIES:
        return VENUE_CITIES[normalized]
    if "gävle" in normalized or "gevalia" in normalized:
        return "Gävle"
    return None


def _description(soup: BeautifulSoup) -> str | None:
    parts = []
    for selector in (".lead", ".mainbody-content"):
        element = soup.select_one(selector)
        if element is not None:
            text = element.get_text("\n", strip=True)
            text = re.sub(r"[ \t\r\f\v]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if text and text not in parts:
                parts.append(text)
    return "\n\n".join(parts) or None


def _parse_detail(session: requests.Session, url: str, listing_year: int) -> dict | None:
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    title_element = soup.select_one("#headingControl")
    datetime_text = _label_value(soup, "Datum och tid")
    venue = _label_value(soup, "Plats/lokal")
    if title_element is None or not datetime_text or not venue:
        log_message(
            "Skipping concert with missing required detail",
            event="crawler_record_skipped",
            url=url,
        )
        return None

    city = _city_for(venue)
    if city is None:
        log_message(
            "Skipping concert with unknown venue city",
            event="crawler_record_skipped",
            url=url,
            venue=venue,
        )
        return None

    try:
        event_date, time_from = _parse_datetime(datetime_text, listing_year)
    except ValueError as error:
        log_message(
            "Skipping concert with invalid date",
            event="crawler_record_skipped",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    title = _clean_text(title_element)
    if not title:
        return None

    return {
        "title": title,
        "date": event_date,
        "url": response.url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": "SE",
        "description": _description(soup),
    }


class GavleSymfoniorkesterCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="gavlesymfoniorkester_se",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="SE",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        session = _session()
        records = []
        for url, listing_year in _detail_urls(session):
            record = _parse_detail(session, url, listing_year)
            if record is not None:
                records.append(record)
        return records


def main():
    GavleSymfoniorkesterCrawler().run()


if __name__ == "__main__":
    main()
