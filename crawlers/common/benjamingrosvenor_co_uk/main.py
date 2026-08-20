import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Benjamin Grosvenor"
SOURCE_URL = "https://www.benjamingrosvenor.co.uk/"
CALENDAR_URL = "https://www.benjamingrosvenor.co.uk/calendar"

COUNTRY_CODES = {
    "Belgium": "BE",
    "Canada": "CA",
    "China": "CN",
    "Denmark": "DK",
    "France": "FR",
    "Germany": "DE",
    "Hong Kong": "HK",
    "Ireland": "IE",
    "Italy": "IT",
    "Japan": "JP",
    "Netherlands": "NL",
    "Poland": "PL",
    "Portugal": "PT",
    "Switzerland": "CH",
    "UK": "GB",
    "USA": "US",
}

# The calendar often names a well-known venue without separately naming its city.
# These are venue facts, not defaults based on the artist's home location.
VENUE_CITIES = {
    "Barbican": "London",
    "BOZAR, Brussels": "Brussels",
    "Bourgie Hall": "Montreal",
    "Bridgewater Hall": "Manchester",
    "Burghof Lorrach": "Lörrach",
    "Carnegie Hall": "New York",
    "Concertgebouw Bruges": "Bruges",
    "Detmold University of Music": "Detmold",
    "Gulbenkian": "Lisbon",
    "Hamarikyu Ashai Hall": "Tokyo",
    "Het Concertgebouw": "Amsterdam",
    "Hong Kong City Hall Concert Hall": "Hong Kong",
    "Louisiana Museum of Modern Art": "Humlebæk",
    "Lousiana Museum of Modern Art": "Humlebæk",
    "Muziekgebouw": "Amsterdam",
    "National Concert Hall": "Dublin",
    "Polish National Opera": "Warsaw",
    "Princeton University": "Princeton",
    "Royal Festival Hall": "London",
    "Saffron Hall": "Saffron Walden",
    "Shanghai Concert Hall": "Shanghai",
    "Societe de la Musique de La Chaux-de-Fonds": "La Chaux-de-Fonds",
    "St John's College Oxford": "Oxford",
    "Suntory Hall": "Tokyo",
    "Symphony Hall Osaka": "Osaka",
    "Teatro della Pergola": "Florence",
    "The Glasshouse, Gateshead": "Gateshead",
    "Theatre De Champs-Elysees": "Paris",
    "Tonhalle": "Düsseldorf",
    "Wigmore Hall": "London",
    "Zentrum Paul Klee": "Bern",
}

KNOWN_CITIES = {
    "Amsterdam", "Athens", "Bern", "Breinton", "Brussels", "Carmel",
    "Copenhagen", "Detmold", "Dublin", "Düsseldorf", "Edinburgh",
    "Eindhoven", "Florence", "Geneva", "Gateshead", "Heerlen", "Helmsley",
    "Hong Kong", "Le Chaux de Fonds", "Liege", "Lisbon", "London", "Lorrach",
    "Manchester", "Montreal", "New York", "Osaka", "Oxford", "Princeton",
    "San Francisco", "Seattle", "Shanghai", "Stanford", "Tokyo", "Torino",
    "Utrecht",
}

DATE_RE = re.compile(
    r"^(\d{1,2})(?:st|nd|rd|th)?(?:\s*/\s*(\d{1,2})(?:st|nd|rd|th)?)?\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+(\d{4})$",
    re.IGNORECASE,
)


def _clean(text):
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    return " ".join(text.replace("\xa0", " ").split())


def _parse_dates(value):
    # Wix occasionally splits an ordinal suffix across adjacent spans.
    value = re.sub(r"(\d)\s*s\s*t\b", r"\1st", value, flags=re.I)
    value = re.sub(r"(\d)\s+(st|nd|rd|th)\b", r"\1\2", value, flags=re.I)
    value = re.sub(r"^(\d)\s+(\d)(?=\s+[A-Za-z])", r"\1\2", value)
    match = DATE_RE.fullmatch(value)
    if not match:
        return []
    day, second_day, month, year = match.groups()
    month_number = datetime.strptime(month[:3].title(), "%b").month
    days = [day] + ([second_day] if second_day else [])
    result = []
    for item in days:
        try:
            result.append(datetime(int(year), month_number, int(item)).date().isoformat())
        except ValueError:
            return []
    return result


def _blocks(soup):
    container = soup.select_one("#comp-kg0xik3u")
    if container is None:
        raise ValueError("Calendar content container was not found")

    blocks = []
    current = None
    for paragraph in container.select("p"):
        value = _clean(paragraph.get_text(" ", strip=True))
        dates = _parse_dates(value)
        if dates:
            if current:
                blocks.append(current)
            current = {"dates": dates, "lines": []}
        elif current and value and value not in {"Tickets"}:
            if (
                value.startswith("More concerts")
                or value.startswith("as they become published")
                or value.startswith("©")
            ):
                break
            current["lines"].append(value)
    if current:
        blocks.append(current)
    return blocks


def _country(lines):
    for line in reversed(lines):
        if line in COUNTRY_CODES:
            return line, COUNTRY_CODES[line]
    if any("Hong Kong" in line for line in lines):
        return "Hong Kong", "HK"
    return None, None


def _venue_and_city(lines, country_name):
    venue = next((line for line in lines if line in VENUE_CITIES), None)
    city = next((line for line in lines if line in KNOWN_CITIES), None)
    if venue and not city:
        city = VENUE_CITIES[venue]
    if country_name == "Hong Kong" and not city:
        city = "Hong Kong"
    return venue, city


def _title(lines, venue, city, country_name):
    excluded = {venue, city, country_name, None}
    useful = [
        line for line in lines
        if line not in excluded and not line.lower().startswith("conductor")
    ]
    if not useful:
        return f"Benjamin Grosvenor at {venue}"
    return "Benjamin Grosvenor — " + " — ".join(useful[:2])


class BenjaminGrosvenorCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="benjamingrosvenor_co_uk",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "title", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for block in _blocks(soup):
            lines = block["lines"]
            country_name, country_code = _country(lines)
            venue, city = _venue_and_city(lines, country_name)
            if not country_code or not venue or not city:
                log_message(
                    "Skipping calendar entry without defensible geography",
                    event="crawler_record_skipped",
                    date=block["dates"][0],
                    has_country=bool(country_code),
                    has_venue=bool(venue),
                    has_city=bool(city),
                )
                continue

            description = "\n".join(lines)
            title = _title(lines, venue, city, country_name)
            for concert_date in block["dates"]:
                records.append({
                    "title": title,
                    "date": concert_date,
                    "url": CALENDAR_URL,
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                })

        log_message(
            "Parsed concert calendar",
            event="crawler_scrape_completed",
            url=CALENDAR_URL,
            record_count=len(records),
        )
        return records


def main():
    BenjaminGrosvenorCrawler().run()


if __name__ == "__main__":
    main()
