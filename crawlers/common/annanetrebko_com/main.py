import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Anna Netrebko"
SOURCE_URL = "https://annanetrebko.com/"
SCHEDULE_URL = f"{SOURCE_URL}schedule/"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        start=1,
    )
}

# The artist tours internationally. The schedule generally appends the city to
# the venue, but some famous venue names are listed without it.
VENUE_LOCATIONS = {
    "arena di verona": ("Verona", "IT"),
    "wiener konzerthaus": ("Vienna", "AT"),
    "wiener staatsoper": ("Vienna", "AT"),
    "teatro dell'opera di roma": ("Rome", "IT"),
    "hungarian state opera": ("Budapest", "HU"),
    "philharmonie essen": ("Essen", "DE"),
    "opernhaus zürich": ("Zurich", "CH"),
    "grimaldi forum, opera monte carlo": ("Monaco", "MC"),
}

CITY_COUNTRIES = {
    "Barcelona": "ES",
    "Basel": "CH",
    "Berlin": "DE",
    "Bucharest": "RO",
    "Budapest": "HU",
    "Essen": "DE",
    "Firenze": "IT",
    "Florence": "IT",
    "Freiburg": "DE",
    "London": "GB",
    "Madrid": "ES",
    "Mannheim": "DE",
    "Milan": "IT",
    "Monaco": "MC",
    "Munich": "DE",
    "Naples": "IT",
    "Paris": "FR",
    "Rome": "IT",
    "Sevilla": "ES",
    "Seville": "ES",
    "Verona": "IT",
    "Vienna": "AT",
    "Zurich": "CH",
    "Zürich": "CH",
}


def parse_dates(value: str) -> list[str]:
    """Expand schedule values such as 'March 31 & April 3 2027'."""
    normalized = " ".join(value.split())
    year_match = re.search(r"\b(20\d{2})\b", normalized)
    if not year_match:
        return []

    year = int(year_match.group(1))
    text = normalized[:year_match.start()]
    token_pattern = "|".join(MONTHS)
    tokens = re.findall(rf"\b(?:{token_pattern})\b|\b\d{{1,2}}\b", text, re.I)
    month = None
    dates = []
    for token in tokens:
        if token.lower() in MONTHS:
            month = MONTHS[token.lower()]
            continue
        if month is None:
            continue
        try:
            parsed = datetime(year, month, int(token))
        except ValueError:
            continue
        dates.append(parsed.date().isoformat())
    return dates


def resolve_location(venue_text: str) -> tuple[str, str, str] | None:
    venue = " ".join(venue_text.split()).strip(" ,")
    known = VENUE_LOCATIONS.get(venue.casefold())
    if known:
        return venue, known[0], known[1]

    if "," not in venue:
        return None
    venue_name, city = (part.strip() for part in venue.rsplit(",", 1))
    country_code = CITY_COUNTRIES.get(city)
    if not venue_name or not country_code:
        return None
    return venue_name, city, country_code


class AnnaNetrebkoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="annanetrebko_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "url", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching schedule", event="crawler_url_fetch", url=SCHEDULE_URL)
        response = requests.get(SCHEDULE_URL, headers=REQUEST_HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for item in soup.select(".schedule-item"):
            title_node = item.select_one("h2")
            date_node = item.select_one("time")
            venue_node = item.select_one("p")
            link = item.select_one("a[href]")
            if not all((title_node, date_node, venue_node, link)):
                continue

            location = resolve_location(venue_node.get_text(" ", strip=True))
            dates = parse_dates(date_node.get_text(" ", strip=True))
            title = title_node.get_text(" ", strip=True)
            url = link.get("href", "").strip()
            if not location or not dates or not title or not url:
                continue

            venue, city, country_code = location
            for event_date in dates:
                records.append({
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": None,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": None,
                })

        log_message(
            "Schedule parsed",
            event="crawler_scrape_completed",
            url=SCHEDULE_URL,
            record_count=len(records),
        )
        return records


def main():
    AnnaNetrebkoCrawler().run()


if __name__ == "__main__":
    main()
