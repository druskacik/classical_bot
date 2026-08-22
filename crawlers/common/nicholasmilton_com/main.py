"""Crawler for Nicholas Milton's international performance calendar."""

import re
from datetime import date
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Nicholas Milton"
SOURCE_URL = "https://www.nicholasmilton.com/"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar.html")
ARCHIVE_URL = urljoin(SOURCE_URL, "past-performances.html")

HEADERS = {
    "User-Agent": "ClassicalBot/1.0 (+https://classical.bot/)",
    "Accept-Language": "en,de;q=0.8",
}

# The site publishes only a venue string, not a postal address or country.  Its
# international schedule repeatedly uses this finite set of cities.  Longer
# aliases come first so that, for example, Osterode am Harz is not shortened.
CITY_COUNTRIES = {
    "Bad Salzuflen": "DE", "Bad Sooden-Allendorf": "DE", "Bad Sooden Allendorf": "DE",
    "Osterode am Harz": "DE", "Hann. Münden": "DE", "Hann Münden": "DE",
    "Alfeld/Leine": "DE", "Alfeld / Leine": "DE", "Groß Schneen": "DE",
    "Neustrelitz": "DE", "Neubrandenburg": "DE", "Wolfenbüttel": "DE",
    "Lüdenscheid": "DE", "Bad Pyrmont": "DE", "Bad Münder": "DE",
    "Göttingen": "DE", "Holzminden": "DE", "Bückeburg": "DE", "Unterlüß": "DE",
    "Wunstorf": "DE", "Einbeck": "DE", "Uelzen": "DE", "Northeim": "DE",
    "Duderstadt": "DE", "Salzgitter-Bad": "DE", "Salzgitter": "DE",
    "Bleckede": "DE", "Weilburg": "DE", "Chemnitz": "DE", "Köln": "DE",
    "Coesfeld": "DE", "Wolfsburg": "DE", "Rostock": "DE", "Güstrow": "DE",
    "Baden-Baden": "DE", "Solingen": "DE", "Remscheid": "DE", "Berlin": "DE",
    "Hannover": "DE", "Limburg": "DE", "Villach": "AT", "Klagenfurt": "AT",
    "Graz": "AT", "Kufstein": "AT", "Ried im Innkreis": "AT",
    "Sydney": "AU", "Canberra": "AU", "Amsterdam": "NL", "Heerlen": "NL",
    "Utrecht": "NL", "Eindhoven": "NL", "Rotterdam": "NL", "Dubrovnik": "HR",
    "Schleswig": "DE", "Flensburg": "DE", "Husum": "DE", "Rendsburg": "DE",
    "Melle": "DE", "Hameln": "DE", "Marburg": "DE", "Hof": "DE",
    "Minden": "DE", "Witzenhausen": "DE", "Herford": "DE", "Weikersheim": "DE",
    "Jena": "DE", "Sofia": "BG", "Ōtsu": "JP", "Otsu": "JP", "Málaga": "ES",
}

# Venue-only labels observed in the archive.  These are used only where the
# venue uniquely identifies a city; ambiguous names such as "Stadthalle" are
# deliberately not inferred.
VENUE_LOCATIONS = {
    "Sydney Opera House": ("Sydney", "AU"),
    "City Recital Hall": ("Sydney", "AU"),
    "Sydney Conservatorim of Music": ("Sydney", "AU"),
    "The Concourse": ("Sydney", "AU"),
    "Bulgaria Hall": ("Sofia", "BG"),
    "Biwako Hall": ("Otsu", "JP"),
    "Deutsche Oper Berlin": ("Berlin", "DE"),
    "Stefaniensaal Graz": ("Graz", "AT"),
    "Stadttheater Klagenfurt": ("Klagenfurt", "AT"),
    "Konzerthaus Klagenfurt": ("Klagenfurt", "AT"),
    "Congress Center Villach": ("Villach", "AT"),
    "Concertgebouw": ("Amsterdam", "NL"),
    "Tivoli Vredenburg": ("Utrecht", "NL"),
    "Parkstad Limburg Theaters": ("Heerlen", "NL"),
    "Teatro Municipal Miguel de Cervantes": ("Málaga", "ES"),
}


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = value.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    result = "\n".join(line for line in lines if line)
    return result or None


def parse_location(value: str | None) -> tuple[str, str, str] | None:
    location = clean_text(value)
    if not location:
        return None
    # Status labels and street addresses occasionally share the location field.
    # They are useful page context but are not venue names.
    location = re.sub(r"\s*CANCELLED\s+DUE\s+TO\s+COVID-19\s*$", "", location, flags=re.I)
    location = re.sub(r"\s*\(\s*\)\s*", " ", location)

    # Parenthesized cities are common in Australian entries.
    parenthesized = re.search(r"\(([^()]+)\)\s*$", location)
    candidates = [parenthesized.group(1).strip()] if parenthesized else []
    candidates.extend(
        city for city in sorted(CITY_COUNTRIES, key=len, reverse=True)
        if re.search(rf"(?<!\w){re.escape(city)}(?!\w)", location, re.I)
    )
    for candidate in candidates:
        canonical = next(
            (city for city in CITY_COUNTRIES if city.casefold() == candidate.casefold()),
            candidate,
        )
        country_code = CITY_COUNTRIES.get(canonical)
        if not country_code:
            continue
        venue = re.sub(rf"\s*\({re.escape(candidate)}\)\s*$", "", location, flags=re.I)
        venue = re.sub(rf"(?<!\w){re.escape(candidate)}(?!\w)", "", venue, flags=re.I)
        venue = re.sub(r"^[\s,|·\-]+|[\s,|·\-]+$", "", venue).strip()
        venue = re.sub(
            r"\s*,?\s+[A-ZÄÖÜ][\wÄÖÜäöüß.-]*(?:straße|str\.|allee|weg)\s+\d.*$",
            "",
            venue,
            flags=re.I,
        )
        venue = re.sub(r"\s*,\s*\d{5}.*$", "", venue)
        venue = re.sub(r"\s{2,}", " ", venue).strip(" ,|·-")
        if venue and venue.casefold() != canonical.casefold():
            return venue, canonical, country_code

    for venue_fragment, (city, country_code) in VENUE_LOCATIONS.items():
        if venue_fragment.casefold() in location.casefold():
            return location, city, country_code
    return None


def parse_event(node, page_url: str) -> dict | None:
    time_node = node.select_one('time[itemprop="startDate"]')
    title_node = node.select_one('[itemprop="name"]')
    url_node = node.select_one('a[itemprop="url"][href]')
    location_node = node.select_one('[itemprop="location"]')
    if not all((time_node, title_node, url_node, location_node)):
        return None

    start = (time_node.get("datetime") or "").strip()
    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except ValueError:
        return None

    location = parse_location(location_node.get_text(" ", strip=True))
    title = clean_text(title_node.get_text(" ", strip=True))
    url = urljoin(page_url, url_node.get("href"))
    if not title or not url or not location:
        return None
    venue, city, country_code = location

    description_node = node.select_one('[itemprop="description"]')
    description = clean_text(description_node.get_text("\n", strip=True)) if description_node else None
    time_from = None
    if len(start) >= 16 and re.fullmatch(r"\d{2}:\d{2}", start[11:16]):
        time_from = start[11:16]

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class NicholasMiltonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nicholasmilton_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _fetch(self, url: str, params: dict | None = None) -> BeautifulSoup:
        log_message("Fetching Nicholas Milton calendar", event="crawler_url_fetch", url=url)
        response = self.session.get(url, params=params, timeout=45)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    @staticmethod
    def _records(soup: BeautifulSoup, page_url: str) -> list[dict]:
        records = []
        for node in soup.select('[itemscope][itemtype="http://schema.org/Event"]'):
            record = parse_event(node, page_url)
            if record:
                records.append(record)
        return records

    def scrape(self) -> list[dict]:
        calendar = self._fetch(CALENDAR_URL)
        page_numbers = {1}
        for link in calendar.select('a[href*="page_e8="]'):
            query = parse_qs(urlparse(link.get("href", "")).query)
            try:
                page_numbers.add(int(query["page_e8"][0]))
            except (KeyError, ValueError, TypeError):
                continue

        records = self._records(calendar, CALENDAR_URL)
        for page in sorted(page_numbers - {1}):
            soup = self._fetch(CALENDAR_URL, {"page_e8": page})
            records.extend(self._records(soup, f"{CALENDAR_URL}?page_e8={page}"))

        archive = self._fetch(ARCHIVE_URL)
        years = set()
        for link in archive.select('a[href*="year="]'):
            query = parse_qs(urlparse(link.get("href", "")).query)
            try:
                years.add(int(query["year"][0]))
            except (KeyError, ValueError, TypeError):
                continue
        # The selected year is plain text rather than a link.
        years.add(date.today().year)
        for year in sorted(years):
            soup = archive if year == date.today().year else self._fetch(ARCHIVE_URL, {"year": year})
            records.extend(self._records(soup, f"{ARCHIVE_URL}?year={year}"))

        unique = {}
        for record in records:
            unique[(record["url"], record["date"], record["time_from"])] = record
        result = sorted(unique.values(), key=lambda item: (item["date"], item["time_from"] or "", item["title"]))
        log_message(
            "Nicholas Milton calendar scrape completed",
            event="crawler_scrape_completed",
            record_count=len(result),
            page_count=len(page_numbers),
            archive_year_count=len(years),
        )
        return result


def main():
    NicholasMiltonCrawler().run()


if __name__ == "__main__":
    main()
