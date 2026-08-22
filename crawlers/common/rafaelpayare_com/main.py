import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Rafael Payare"
SOURCE_URL = "https://rafaelpayare.com/"
CALENDAR_URL = "https://rafaelpayare.com/calendar/"

# The calendar's location field alternates between cities and venue names.
# These first-party names and unambiguous orchestra homes let us normalize both.
LOCATIONS = {
    "WARSAW PHILHARMONIC CONCERT HALL": ("Warsaw Philharmonic Concert Hall", "Warsaw", "PL"),
    "MUSIKHUSET AARHUS": ("Musikhuset Aarhus", "Aarhus", "DK"),
    "ELBPHILHARMONIE HAMBURG": ("Elbphilharmonie Hamburg", "Hamburg", "DE"),
    "PAN-CAUCASIAN YOUTH ORCHESTRA": ("Tsinandali Estate Amphitheatre", "Tsinandali", "GE"),
    "MONTREAL, CANADA": ("Maison symphonique de Montréal", "Montreal", "CA"),
    "SAN DIEGO, CA": ("Jacobs Music Center", "San Diego", "US"),
    "DAVIES SYMPHONY HALL": ("Davies Symphony Hall", "San Francisco", "US"),
    "WALT DISNEY CONCERT HALL": ("Walt Disney Concert Hall", "Los Angeles", "US"),
    "JACOBS MUSIC CENTER": ("Jacobs Music Center", "San Diego", "US"),
    "NEW YORK, NY": ("David Geffen Hall", "New York", "US"),
    "PARIS, FRANCE": ("Philharmonie de Paris", "Paris", "FR"),
    "PALM SPRINGS FRIENDS OF PHILHARMONIC": ("McCallum Theatre", "Palm Desert", "US"),
    "PALAU DE LA MÚSICA": ("Palau de la Música Catalana", "Barcelona", "ES"),
    "LA HALLE AUX GRAINS": ("Halle aux Grains", "Toulouse", "FR"),
    "LA SEINE MUSICALE": ("La Seine Musicale", "Boulogne-Billancourt", "FR"),
    "NATIONAL CONCERT HALL DUBLIN": ("National Concert Hall", "Dublin", "IE"),
    "SAN FRANCISCO, CA": ("Davies Symphony Hall", "San Francisco", "US"),
}


def parse_dates(value: str) -> list[str]:
    """Expand calendar labels such as '4, 5 & 7 NOVEMBER 2026'."""
    match = re.fullmatch(r"(.+?)\s+([A-Z]+)\s+(\d{4})", value.strip().upper())
    if not match:
        raise ValueError(f"Unsupported date label: {value!r}")
    days_text, month, year = match.groups()
    days = [int(day) for day in re.findall(r"\d+", days_text)]
    return [datetime.strptime(f"{day} {month} {year}", "%d %B %Y").date().isoformat() for day in days]


class RafaelPayareCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="rafaelpayare_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "url"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        for item in soup.select("li.event-item"):
            date_node = item.select_one(".date-title")
            title_node = item.select_one(".perform-title")
            location_node = item.select_one(".city")
            link = item.select_one("a.ticket-link[href]")
            if not all((date_node, title_node, location_node, link)):
                continue

            location_key = location_node.get_text(" ", strip=True).upper()
            resolved = LOCATIONS.get(location_key)
            if resolved is None:
                log_message(
                    "Skipping event with unresolved location",
                    event="crawler_record_skipped",
                    url=link.get("href"),
                    location=location_key,
                )
                continue

            venue, city, country_code = resolved
            title = title_node.get_text(" ", strip=True)
            notes_node = item.select_one(".notes")
            description = notes_node.get_text("\n", strip=True) if notes_node else None
            url = link.get("href", "").strip()
            if not url:
                continue

            try:
                dates = parse_dates(date_node.get_text(" ", strip=True))
            except ValueError as error:
                log_message(
                    "Skipping event with invalid date",
                    event="crawler_record_skipped",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            for event_date in dates:
                records.append(
                    {
                        "title": title,
                        "date": event_date,
                        "url": url,
                        "time_from": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": description or None,
                    }
                )

        log_message("Calendar parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    RafaelPayareCrawler().run()


if __name__ == "__main__":
    main()
