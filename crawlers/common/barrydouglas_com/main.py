import calendar
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Barry Douglas"
SOURCE_URL = "http://barrydouglas.com/"
TOUR_URL = urljoin(SOURCE_URL, "index.php/on-tour/")

# The touring diary is international and its older entries are quite terse.  These
# mappings cover locations used by the diary while avoiding guesses from an artist,
# orchestra, or country name alone.
COUNTRY_ALIASES = {
    "armenia": "AM", "australia": "AU", "brazil": "BR", "china": "CN",
    "croatia": "HR", "england": "GB", "finland": "FI", "france": "FR",
    "germany": "DE", "hong kong": "HK", "india": "IN", "ireland": "IE",
    "italy": "IT", "lithuania": "LT", "macedonia": "MK",
    "netherlands": "NL", "poland": "PL", "slovenia": "SI", "spain": "ES",
    "uk": "GB", "united kingdom": "GB", "united states": "US", "uruguay": "UY",
    "usa": "US",
}

CITY_COUNTRIES = {
    "Amsterdam": "NL", "Bantry": "IE", "Baoji": "CN", "Belfast": "GB",
    "Bellingham": "US", "Birmingham": "GB", "Bradford": "GB",
    "Brisbane": "AU", "Carmel": "US", "Catania": "IT", "Changsha": "CN",
    "Changshu": "CN", "Cork": "IE", "Coventry": "GB", "Dinard": "FR",
    "Dublin": "IE", "Ede": "NL", "Edinburgh": "GB", "Enniskillen": "GB",
    "Eugene": "US", "Fresno": "US", "Gdansk": "PL", "Genova": "IT",
    "Guildford": "GB", "Halle": "DE", "Hamburg": "DE", "Helsinki": "FI",
    "Houston": "US", "Jacksonville": "US", "Kilkenny": "IE", "Limerick": "IE",
    "Lille": "FR", "London": "GB", "Manassas": "US", "Mannheim": "DE",
    "Minato-ku": "JP", "Monkstown": "IE", "Montevideo": "UY", "Mumbai": "IN",
    "Naantali": "FI", "Nancy": "FR", "Neuilly-sur-Seine": "FR",
    "Newbury": "GB", "New Ross": "IE", "Nuremberg": "DE", "Nürnberg": "DE",
    "Ohrid": "MK", "Oxford": "GB", "Papendorf": "DE", "Paris": "FR",
    "Perth": "GB", "Prague": "CZ", "Sligo": "IE", "Tallinn": "EE",
    "Tallin": "EE", "Tokyo": "JP", "Troy": "US", "Vilnius": "LT",
    "Vitoria": "ES", "Warsaw": "PL", "Wexford": "IE", "Zagreb": "HR",
    "Zhuzhou": "CN", "Zorneding": "DE",
}

VENUE_CITY_HINTS = {
    "aram khachaturian concert hall": ("Yerevan", "AM"),
    "cadogan hall": ("London", "GB"),
    "carnegie hall": ("New York", "US"),
    "edesche concertzaal": ("Ede", "NL"),
    "eesti kontsert": ("Tallinn", "EE"),
    "fairfield halls": ("Croydon", "GB"),
    "national concert hall": ("Dublin", "IE"),
    "national centre for the performing arts": ("Mumbai", "IN"),
    "perth concert hall": ("Perth", "GB"),
    "polish baltic philharmonic": ("Gdansk", "PL"),
    "romanian athenaeum": ("Bucharest", "RO"),
    "salle gaveau": ("Paris", "FR"),
    "shanxi grand theatre": ("Taiyuan", "CN"),
    "theatre royal norwich": ("Norwich", "GB"),
    "ulster hall": ("Belfast", "GB"),
    "usher hall": ("Edinburgh", "GB"),
    "waalse kerk": ("Amsterdam", "NL"),
    "wigmore hall": ("London", "GB"),
}

VENUE_WORDS = re.compile(
    r"\b(?:archive|arts centre|arts center|athenaeum|auditorium|cathedral|church|"
    r"concert hall|concertzaal|corum|estate|hall|kerk|opera|palais|stadl|synagogue|"
    r"theatre|theater)\b",
    re.I,
)


def clean_text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def parse_location(raw: str) -> tuple[str, str, str] | None:
    text = re.sub(r"\s+", " ", raw).strip(" ,")
    folded = text.casefold()

    city = None
    country_code = None
    for candidate, code in sorted(CITY_COUNTRIES.items(), key=lambda item: -len(item[0])):
        if re.search(rf"(?<!\w){re.escape(candidate.casefold())}(?!\w)", folded):
            city, country_code = candidate, code
            break

    for alias, code in COUNTRY_ALIASES.items():
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", folded):
            country_code = code
            break

    for hint, (hint_city, hint_country) in VENUE_CITY_HINTS.items():
        if hint in folded:
            city = city or hint_city
            country_code = country_code or hint_country
            break

    if not city or not country_code:
        return None

    parts = [part.strip() for part in text.split(",") if part.strip()]
    venue = next((part for part in parts[:-1] if VENUE_WORDS.search(part)), "")
    if not venue:
        venue = next((part for part in parts if any(hint in part.casefold() for hint in VENUE_CITY_HINTS)), "")
    # A bare city is location evidence, not a venue.
    if venue.casefold() == city.casefold() or not venue:
        return None
    return venue, city, country_code


def parse_occurrence(month_year: str, raw: str) -> tuple[str, str | None] | None:
    # Ranges on this site include festivals, residencies, and masterclasses and do
    # not identify a concrete concert occurrence, so they are intentionally skipped.
    if re.search(r"[-–—]|\bto\b", raw, re.I):
        return None
    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", raw, re.I)
    heading = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", month_year.strip())
    if not match or not heading:
        return None
    month = list(calendar.month_name).index(heading.group(1))
    try:
        event_date = date(int(heading.group(2)), month, int(match.group(1)))
    except ValueError:
        return None
    # Single-digit hours are ambiguous on this diary (e.g. "3:30" with no
    # AM/PM), so retain only unambiguous 24-hour-looking values.
    time_match = re.search(r"\b([01]\d|2[0-3])[:.]([0-5]\d)\b", raw)
    time_from = f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else None
    return event_date.isoformat(), time_from


class BarryDouglasCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="barrydouglas_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching Barry Douglas tour diary", event="crawler_url_fetch", url=TOUR_URL)
        response = requests.get(TOUR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        records = []

        for heading in soup.select("h1.section-title"):
            month_year = clean_text(heading)
            month = heading.find_next_sibling("div", class_="tour-month")
            if month is None:
                continue
            for event in month.find_all("div", class_="tour-date", recursive=False):
                date_node = event.select_one(".date-time")
                location_node = event.select_one(".venue-citycountry")
                repertoire_node = event.select_one(".repertoire")
                if not date_node or not location_node:
                    continue
                occurrence = parse_occurrence(month_year, clean_text(date_node))
                location = parse_location(clean_text(location_node))
                if not occurrence or not location:
                    continue

                event_date, time_from = occurrence
                venue, city, country_code = location
                repertoire = clean_text(repertoire_node) if repertoire_node else ""
                performers_node = event.select_one(".orchestra-performers")
                performers = clean_text(performers_node) if performers_node else ""
                description_parts = [part for part in (repertoire, performers) if part]
                description = "\n\n".join(description_parts) or None
                title_detail = (performers or repertoire).split("\n", 1)[0][:180].rstrip()
                title = f"Barry Douglas — {title_detail}" if title_detail else f"Barry Douglas at {venue}"
                link = event.select_one(".moreinfo-buy a[href]")
                event_url = urljoin(TOUR_URL, link["href"]) if link else TOUR_URL

                records.append({
                    "title": title,
                    "date": event_date,
                    "url": event_url,
                    "time_from": time_from,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                })

        log_message(
            "Parsed Barry Douglas tour diary",
            event="crawler_parse_completed",
            url=TOUR_URL,
            record_count=len(records),
        )
        return records


def main():
    BarryDouglasCrawler().run()


if __name__ == "__main__":
    main()
