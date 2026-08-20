import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Antony Hermus"
SOURCE_URL = "https://www.antonyhermus.com/"
AGENDA_URL = "https://www.antonyhermus.com/agenda"
TIMEZONE = ZoneInfo("Europe/Amsterdam")

COUNTRY_ALIASES = {
    "A": "AT", "AUSTRIA": "AT", "AUSTRALIA": "AU", "B": "BE",
    "BELGIUM": "BE", "CA": "CA", "CANADA": "CA", "CH": "CH",
    "CZ": "CZ", "D": "DE", "DE": "DE", "DENMARK": "DK", "DK": "DK",
    "E": "ES", "ES": "ES", "F": "FR", "FI": "FI", "FINLAND": "FI",
    "FR": "FR", "GB": "GB", "GERMANY": "DE", "I": "IE", "IE": "IE",
    "IRELAND": "IE", "N": "NO", "N-I": "GB", "NL": "NL", "NO": "NO",
    "NORWAY": "NO", "NZ": "NZ", "P": "PL", "PL": "PL", "R": "RO",
    "RO": "RO", "S": "SE", "SE": "SE", "SPAIN": "ES", "UK": "GB",
    "USA": "US", "US": "US",
}

# The event editor usually puts a free-form venue/city line in italics rather
# than filling Squarespace's location fields. These mappings cover recurring
# halls and city spellings in the published archive without treating a city as
# a venue.
CITY_COUNTRIES = {
    "AALBORG": "DK", "AMSTERDAM": "NL", "APELDOORN": "NL", "ARNHEM": "NL",
    "AUCKLAND": "NZ", "BARCELONA": "ES", "BELFAST": "GB", "BERGEN": "NO",
    "BERLIN": "DE", "BRATISLAVA": "SK", "BRUSSELS": "BE", "BUCHAREST": "RO",
    "CARDIFF": "GB", "COPENHAGEN": "DK", "DARWIN": "AU", "DEN HAAG": "NL",
    "DESSAU": "DE", "DUBLIN": "IE", "ENSCHEDE": "NL", "GLASGOW": "GB",
    "HELSINKI": "FI", "HUDDERSFIELD": "GB", "HULL": "GB", "INNSBRUCK": "AT",
    "KATOWICE": "PL", "LEEDS": "GB", "LE HAVRE": "FR", "LEUVEN": "BE",
    "LIVERPOOL": "GB", "LONDON": "GB", "MANCHESTER": "GB", "MONTREAL": "CA",
    "NAMUR": "BE", "NIJMEGEN": "NL", "NOTTINGHAM": "GB", "OFFENBURG": "DE",
    "PARIS": "FR", "PERTH": "GB", "ROUEN": "FR", "SEOUL": "KR",
    "STRASBOURG": "FR", "SWANSEA": "GB", "SYDNEY": "AU", "TENERIFE": "ES",
    "TRONDHEIM": "NO", "VANCOUVER": "CA",
}

VENUE_CITIES = {
    "AMARE": "Den Haag", "ATHENEAUM BUCHAREST": "Bucharest",
    "AUDITORIO DE TENERIFE ADÁN MARTÍN": "Tenerife", "BOZAR": "Brussels",
    "BRANGWYN HALL": "Swansea", "BRIDGEWATER HALL": "Manchester",
    "CITY HALLS": "Glasgow", "CONCERTGEBOUW": "Amsterdam",
    "HODDINOTT HALL": "Cardiff", "HULL TOWN HALL": "Hull",
    "LOTTE CONCERT HALL": "Seoul",
    "MUSIS PARKZAAL": "Arnhem", "NATIONAL CONCERT HALL DUBLIN": "Dublin",
    "OPERA DE ROUEN": "Rouen", "ORPHEUM": "Vancouver",
    "PHILHARMONIE DE PARIS": "Paris", "ROYAL FESTIVAL HALL": "London",
    "TOWN HALL": "Auckland", "WILMINKTHEATER": "Enschede",
}


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip(" ,")


def _country(value):
    return COUNTRY_ALIASES.get(_clean(value).upper())


def _parse_place(body):
    soup = BeautifulSoup(body or "", "html.parser")
    emphasized = [_clean(tag.get_text(" ", strip=True)) for tag in soup.find_all(["em", "i"])]
    place = next((text for text in reversed(emphasized) if text), "")
    if not place:
        return None

    venue_part = place
    city = None
    country_code = None
    match = re.search(r"\(([^()]*)\)\s*$", place)
    if match:
        venue_part = _clean(place[:match.start()])
        bits = [_clean(bit) for bit in match.group(1).split(",")]
        country_code = _country(bits[-1])
        if len(bits) > 1:
            city = bits[-2].title()

    upper_venue = venue_part.upper()
    if not city and upper_venue in VENUE_CITIES:
        city = VENUE_CITIES[upper_venue]
    if not city:
        for known_city in sorted(CITY_COUNTRIES, key=len, reverse=True):
            if upper_venue == known_city:
                # A city-only label does not establish a venue.
                return None
            if upper_venue.startswith(known_city + " "):
                city = known_city.title()
                venue_part = _clean(venue_part[len(known_city):])
                upper_venue = venue_part.upper()
                break
            if upper_venue.endswith(" " + known_city):
                city = known_city.title()
                venue_part = _clean(venue_part[:-len(known_city)])
                upper_venue = venue_part.upper()
                break

    if city and not country_code:
        country_code = CITY_COUNTRIES.get(city.upper())
    if not city or not country_code or not venue_part:
        return None
    if venue_part.casefold() == city.casefold():
        return None
    return venue_part, city, country_code


def _description(body):
    text = BeautifulSoup(body or "", "html.parser").get_text("\n", strip=True)
    return re.sub(r"\n{2,}", "\n", text) or None


def _is_concert(item):
    title = _clean(item.get("title")).upper()
    if "MASTERCLASS" in title or "MASTER CLASS" in title:
        return False
    # Multi-day calendar blocks are courses, competitions, or rehearsal periods
    # on this source, not a single advertised public performance.
    start = datetime.fromtimestamp(item["startDate"] / 1000, TIMEZONE)
    end = datetime.fromtimestamp(item["endDate"] / 1000, TIMEZONE)
    return start.date() == end.date()


class AntonyHermusCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="antonyhermus_com",
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
        url = f"{AGENDA_URL}?format=json"

        while url:
            log_message("Fetching agenda page", event="crawler_url_fetch", url=url)
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            payload = response.json()

            for item in (payload.get("upcoming") or []) + (payload.get("past") or []):
                if item.get("id") in seen_ids:
                    continue
                seen_ids.add(item.get("id"))
                if not _is_concert(item):
                    continue
                place = _parse_place(item.get("body"))
                if not place:
                    continue
                venue, city, country_code = place
                start = datetime.fromtimestamp(item["startDate"] / 1000, TIMEZONE)
                end = datetime.fromtimestamp(item["endDate"] / 1000, TIMEZONE)
                event_url = f"{AGENDA_URL}/{item['urlId']}"
                records.append({
                    "title": _clean(item.get("title")),
                    "date": start.date().isoformat(),
                    "url": event_url,
                    "time_from": start.strftime("%H:%M"),
                    "time_to": end.strftime("%H:%M"),
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": _description(item.get("body")),
                })

            pagination = payload.get("pagination") or {}
            next_path = pagination.get("nextPageUrl") if pagination.get("nextPage") else None
            url = f"https://www.antonyhermus.com{next_path}&format=json" if next_path else None

        log_message(
            "Agenda scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    AntonyHermusCrawler().run()


if __name__ == "__main__":
    main()
