import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Jonas Kaufmann"
SOURCE_URL = "https://jonaskaufmann.com/"
CALENDAR_URL = "https://jonaskaufmann.com/kalender/"

# The calendar is international and does not publish country names.  Locations
# not present here are deliberately skipped rather than assigned a guessed
# country.  Additions can be made as new tour cities appear.
CITY_COUNTRIES = {
    "aarhus": "DK",
    "berlin": "DE",
    "bremen": "DE",
    "bukarest": "RO",
    "dresden": "DE",
    "düsseldorf": "DE",
    "essen": "DE",
    "frankfurt": "DE",
    "hamburg": "DE",
    "london": "GB",
    "linz": "AT",
    "madrid": "ES",
    "mannheim": "DE",
    "milano": "IT",
    "münchen": "DE",
    "napoli": "IT",
    "new york": "US",
    "paris": "FR",
    "roma": "IT",
    "salzburg": "AT",
    "sofia": "BG",
    "stuttgart": "DE",
    "wien": "AT",
    "zürich": "CH",
}

# A few venue-led calendar labels omit the city even though the venue name
# identifies it unambiguously.
LOCATION_OVERRIDES = {
    "arena di verona": ("Verona", "Arena di Verona", "IT"),
    "festspiele taggenbrunn": ("Taggenbrunn", "Festspiele Taggenbrunn", "AT"),
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_location(value: str):
    location = clean_text(value)
    override = LOCATION_OVERRIDES.get(location.casefold())
    if override:
        return override

    if "," not in location:
        return None
    city, venue = (part.strip() for part in location.split(",", 1))
    country_code = CITY_COUNTRIES.get(city.casefold())
    if not city or not venue or not country_code:
        return None
    return city, venue, country_code


def parse_dates(value: str):
    dates = []
    for raw_date in re.findall(r"\d{2}\.\d{2}\.\d{4}", value):
        try:
            dates.append(datetime.strptime(raw_date, "%d.%m.%Y").date().isoformat())
        except ValueError:
            log_message(
                "Skipping invalid calendar date",
                event="crawler_record_skipped",
                raw_date=raw_date,
            )
    return dates


class JonasKaufmannCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jonaskaufmann_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "url", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(
            CALENDAR_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        for article in soup.select("article.calendar"):
            title_node = article.select_one('[data-id="aeed2af"]')
            dates_node = article.select_one('[data-id="0513042"]')
            location_node = article.select_one('[data-id="b25465a"]')
            link_node = article.select_one('[data-id="eaf55cb"] a[href]')
            if not all((title_node, dates_node, location_node, link_node)):
                log_message(
                    "Skipping incomplete calendar entry",
                    event="crawler_record_skipped",
                    post_id=article.get("id"),
                )
                continue

            title = clean_text(title_node.get_text(" ", strip=True))
            url = link_node.get("href", "").strip()
            location = parse_location(location_node.get_text(" ", strip=True))
            dates = parse_dates(dates_node.get_text(" ", strip=True))
            if not title or not url or not location or not dates:
                log_message(
                    "Skipping calendar entry with unresolved required fields",
                    event="crawler_record_skipped",
                    post_id=article.get("id"),
                    url=url or CALENDAR_URL,
                )
                continue

            city, venue, country_code = location
            for event_date in dates:
                records.append(
                    {
                        "title": title,
                        "date": event_date,
                        "url": url,
                        "time_from": None,
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": None,
                    }
                )

        log_message(
            "Calendar parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
            url=CALENDAR_URL,
        )
        return records


def main():
    JonasKaufmannCrawler().run()


if __name__ == "__main__":
    main()
