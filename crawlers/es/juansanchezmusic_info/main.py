import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Jan Uve (Juan Sánchez)"
SOURCE_URL = "https://januvemusic.com/"
EVENTS_URL = urljoin(SOURCE_URL, "live-concerts-dates")

# The artist is based in Spain but occasionally tours. The current calendar does
# not expose structured country fields, so cities seen on the first-party event
# listings are mapped explicitly rather than assigning Spain to every concert.
CITY_COUNTRIES = {
    "Barcelona": "ES",
    "London": "GB",
    "Madrid": "ES",
}


def _clean_text(element) -> str | None:
    if element is None:
        return None
    text = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
    return text or None


def _parse_location(location: str, title: str) -> tuple[str, str, str] | None:
    city = next(
        (
            candidate
            for candidate in CITY_COUNTRIES
            if re.search(rf"\b{re.escape(candidate)}\b", f"{location} {title}", re.IGNORECASE)
        ),
        None,
    )
    if city is None:
        return None

    venue = location.split(",", 1)[0].strip()
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city, CITY_COUNTRIES[city]


def _parse_event(article) -> dict | None:
    title_link = article.select_one("h2.event-title a[href]")
    date_element = article.select_one(".date-long .date")
    time_element = article.select_one(".date-long .time")
    location_element = article.select_one(".event-location")

    title = _clean_text(title_link)
    date_text = _clean_text(date_element)
    location = _clean_text(location_element)
    if not title or not date_text or not location:
        return None

    try:
        event_date = datetime.strptime(date_text, "%A, %B %d, %Y").date().isoformat()
    except ValueError:
        return None

    location_details = _parse_location(location, title)
    if location_details is None:
        return None
    venue, city, country_code = location_details

    time_from = None
    time_text = _clean_text(time_element)
    if time_text:
        try:
            time_from = datetime.strptime(time_text.upper(), "%I:%M%p").time().isoformat()
        except ValueError:
            pass

    description = _clean_text(article.select_one(".event-notes"))
    return {
        "title": title,
        "date": event_date,
        "url": urljoin(SOURCE_URL, title_link["href"]),
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class JuanSanchezMusicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="juansanchezmusic_info",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="ES",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert listing", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(
            EVENTS_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")
        records = []
        skipped_count = 0
        for article in soup.select("article.list-style"):
            record = _parse_event(article)
            if record is None:
                skipped_count += 1
                continue
            records.append(record)

        log_message(
            "Concert listing parsed",
            event="crawler_listing_parsed",
            url=EVENTS_URL,
            record_count=len(records),
            skipped_count=skipped_count,
        )
        return records


def main():
    JuanSanchezMusicCrawler().run()


if __name__ == "__main__":
    main()
