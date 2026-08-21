import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Lotta Wennäkoski"
SOURCE_URL = "https://lottawennakoski.com/"
API_URL = f"{SOURCE_URL}wp-json/tribe/events/v1/events"

# The calendar is international and usually puts the location in the free-text
# description. These mappings also cover venue-only descriptions in the archive.
CITY_COUNTRIES = {
    "Aberdeen": "GB", "Adelaide": "AU", "Amsterdam": "NL", "Ann Arbor": "US",
    "Antwerp": "BE", "Auckland": "NZ", "Augsburg": "DE", "Baltimore": "US",
    "Barcelona": "ES", "Basel": "CH", "Berkeley": "US", "Bergen": "NO",
    "Berlin": "DE", "Billnäs": "FI", "Bodø": "NO", "Boston": "US",
    "Bremen": "DE", "Bruges": "BE", "Brussels": "BE", "Budapest": "HU",
    "Buxton": "GB", "Cologne": "DE", "Copenhagen": "DK", "Costa Mesa": "US",
    "Detroit": "US", "Düsseldorf": "DE", "Edinburgh": "GB", "Edmonton": "CA",
    "Eindhoven": "NL", "Eisenstadt": "AT", "Enontekiö": "FI", "Espoo": "FI",
    "Esbjerg": "DK", "Ettenheim": "DE", "Frankfurt": "DE", "Freiburg": "DE",
    "Gothenburg": "SE", "Glasgow": "GB", "Hamburg": "DE", "Hämeenlinna": "FI",
    "Hannover": "DE", "Heerlen": "NL", "Helsingborg": "SE", "Helsinki": "FI",
    "Houston": "US", "Huddersfield": "GB", "Hyvinkää": "FI", "Järvenpää": "FI",
    "Joensuu": "FI", "Jönköping": "SE", "Jyväskylä": "FI", "Karlskoga": "SE",
    "Kaskinen": "FI", "Katowice": "PL", "Kiel": "DE", "Kittilä": "FI",
    "Klagenfurt": "AT", "Kokkola": "FI", "Kotka": "FI", "Kuhmo": "FI",
    "Kuopio": "FI", "Lahti": "FI", "Lappeenranta": "FI", "Leipzig": "DE",
    "Lidköping": "SE", "Lisbon": "PT", "Lohja": "FI", "London": "GB",
    "Luton": "GB", "Madrid": "ES", "Malmö": "SE", "Manchester": "GB",
    "Mantova": "IT", "Mariehamn": "FI", "Mikkeli": "FI", "Milwaukee": "US",
    "Minneapolis": "US", "Montréal": "CA", "New York": "US", "Netzeband": "DE",
    "Nurmes": "FI", "Oberlin": "US", "Olomouc": "CZ", "Olsberg": "CH",
    "Oslo": "NO", "Oulu": "FI", "Outokumpu": "FI", "Paris": "FR",
    "Pilsen": "CZ", "Pittsburgh": "US", "Pori": "FI", "Porto": "PT",
    "Porvoo": "FI", "Rennes": "FR", "Reykjavik": "IS", "Rome": "IT",
    "Rotterdam": "NL", "Rouen": "FR", "Rovaniemi": "FI", "Salford": "GB",
    "San Diego": "US", "San Francisco": "US", "Santa Barbara": "US",
    "Schwäbisch Gmünd": "DE", "Seattle": "US", "Singen": "DE",
    "Siuntio": "FI", "Sofia": "BG", "St. Louis": "US", "Stockholm": "SE",
    "Stuttgart": "DE", "Suffolk": "GB", "Sundsvall": "SE", "Tallinn": "EE",
    "Tampere": "FI", "Timișoara": "RO", "Timisoara": "RO", "Toholampi": "FI",
    "Trento": "IT", "Tromsø": "NO", "Turku": "FI", "Umeå": "SE",
    "Uppsala": "SE", "Uttersberg": "SE", "Vaasa": "FI", "Vancouver": "CA",
    "Vienna": "AT", "Viitasaari": "FI", "Växjö": "SE", "Warsaw": "PL",
    "Washington D.C.": "US", "Winnipeg": "CA", "Zagreb": "HR",
}

VENUE_LOCATIONS = {
    "Barbican": ("London", "GB"), "Bozar": ("Brussels", "BE"),
    "Bradley Symphony Center": ("Milwaukee", "US"),
    "Davies Symphony Hall": ("San Francisco", "US"),
    "De Singel": ("Antwerp", "BE"), "Elbphilharmonie": ("Hamburg", "DE"),
    "Espoon kulttuurikeskus": ("Espoo", "FI"), "Flagey": ("Brussels", "BE"),
    "Glasgow Royal Concert Hall": ("Glasgow", "GB"),
    "Harpa": ("Reykjavik", "IS"), "Kölner Philharmonie": ("Cologne", "DE"),
    "Konserthuset": ("Stockholm", "SE"), "Korsholm Music Festival": ("Vaasa", "FI"),
    "Kuhmo Chamber Music Festival": ("Kuhmo", "FI"),
    "Maida Vale Studio": ("London", "GB"), "Mikkeli Music Festival": ("Mikkeli", "FI"),
    "Musiikkitalo": ("Helsinki", "FI"), "Music Centre": ("Helsinki", "FI"),
    "Palau de la Música": ("Barcelona", "ES"), "Philharmonie Luxembourg": ("Luxembourg", "LU"),
    "Royal Festival Hall": ("London", "GB"), "Tampere-talo": ("Tampere", "FI"),
    "Tampere Hall": ("Tampere", "FI"), "Usher Hall": ("Edinburgh", "GB"),
    "Vancouver Playhouse": ("Vancouver", "CA"), "Wigmore Hall": ("London", "GB"),
}

COUNTRY_NAMES = {
    "Australia": "AU", "Austria": "AT", "Bulgaria": "BG", "Canada": "CA",
    "Czech Republic": "CZ", "Denmark": "DK", "England": "GB", "Finland": "FI",
    "France": "FR", "Germany": "DE", "Italy": "IT", "Japan": "JP",
    "Netherlands": "NL", "Norway": "NO", "Poland": "PL", "Romania": "RO",
    "Sweden": "SE", "Switzerland": "CH", "UK": "GB", "United States": "US",
    "USA": "US",
}


def _clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _description(value):
    return _clean_text(BeautifulSoup(value or "", "html.parser").get_text(" "))


def _location_text(description):
    location = re.split(r"\s+[–—]\s+", description, maxsplit=1)[0]
    location = re.sub(
        r"^(?:\d{1,2}(?:[.\-/]\d{1,2})?.*?|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d+.*?)\s+[–—]\s+",
        "", location, flags=re.I,
    )
    location = re.sub(r"^(?:Finnish|Dutch|U\.S\.)?\s*premier\s+", "", location, flags=re.I)
    return location.strip(" ,.-")


def _place(description):
    location = _location_text(description)
    for venue, (city, country_code) in sorted(VENUE_LOCATIONS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<!\w){re.escape(venue)}(?!\w)", location, re.I):
            return venue, city, country_code

    for city, country_code in sorted(CITY_COUNTRIES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<!\w){re.escape(city)}(?!\w)", location, re.I):
            venue = re.sub(rf"(?:,\s*|\s+){re.escape(city)}(?:\s*,.*)?$", "", location, flags=re.I).strip(" ,.-")
            invalid_venue = (
                re.search(r"\b(?:19|20)\d{2}\b|\bpremier\b", venue, re.I)
                or re.search(r"\b(?:festival|biennale|musiikkijuhlat)\b", venue, re.I)
                or venue.casefold() == "musica nova"
            )
            if venue and venue.casefold() != city.casefold() and not invalid_venue:
                return venue, city, country_code

    # Country-only locations are useful only when the preceding comma-separated
    # component gives a distinct venue and a mapped city can be found there.
    for country, country_code in COUNTRY_NAMES.items():
        if re.search(rf"(?<!\w){re.escape(country)}(?!\w)", location, re.I):
            pieces = [piece.strip() for piece in location.split(",")]
            if len(pieces) >= 3 and pieces[-2] in CITY_COUNTRIES:
                venue = ", ".join(pieces[:-2]).strip()
                if venue:
                    return venue, pieces[-2], country_code
    return None


def _record(event):
    title = _clean_text(event.get("title"))
    description = _description(event.get("description"))
    start = event.get("start_date_details") or {}
    try:
        event_date = date(int(start["year"]), int(start["month"]), int(start["day"])).isoformat()
    except (KeyError, TypeError, ValueError):
        return None
    place = _place(description)
    if not title or not event.get("url") or not description or not place:
        return None
    venue, city, country_code = place
    return {
        "title": title,
        "date": event_date,
        "url": event["url"],
        "time_from": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class LottaWennakoskiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="lottawennakoski_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "venue"],
    )

    def scrape(self):
        records = []
        page = 1
        total_pages = None
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalBot/1.0)"
        while total_pages is None or page <= total_pages:
            params = {
                "start_date": "1900-01-01",
                "end_date": "2100-12-31",
                "per_page": 50,
                "page": page,
            }
            log_message("Fetching calendar API page", event="crawler_url_fetch", url=API_URL, page=page)
            response = session.get(API_URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            total_pages = int(payload.get("total_pages") or 1)
            for event in payload.get("events") or []:
                record = _record(event)
                if record:
                    records.append(record)
            page += 1
        log_message("Calendar parsed", event="crawler_records_parsed", record_count=len(records))
        return records


def main():
    LottaWennakoskiCrawler().run()


if __name__ == "__main__":
    main()
