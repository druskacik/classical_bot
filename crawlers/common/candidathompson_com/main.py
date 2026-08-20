import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Candida Thompson"
SOURCE_URL = "https://www.candidathompson.com/"
ARCHIVE_URL = urljoin(SOURCE_URL, "concerts/?archive=1")

# The diary is Dutch, but Candida Thompson tours internationally.  The site does
# not expose structured geography, so explicit country names and tour cities take
# precedence over the Dutch default.
COUNTRY_MARKERS = {
    "albania": "AL", "austria": "AT", "belgium": "BE", "canada": "CA",
    "china": "CN", "croatia": "HR", "czech": "CZ", "denmark": "DK",
    "estonia": "EE", "finland": "FI", "france": "FR", "germany": "DE",
    "hungary": "HU", "italy": "IT", "japan": "JP", "latvia": "LV",
    "lithuania": "LT", "luxembourg": "LU", "norway": "NO", "poland": "PL",
    "portugal": "PT", "romania": "RO", "russia": "RU", "slovakia": "SK",
    "slovenia": "SI", "spain": "ES", "sweden": "SE", "switzerland": "CH",
    "turkey": "TR", "uk": "GB", "united kingdom": "GB", "usa": "US",
}

CITY_COUNTRIES = {
    "Antwerp": "BE", "Barcelona": "ES", "Basel": "CH", "Berlin": "DE",
    "Bern": "CH", "Bilbao": "ES", "Bilboa": "ES", "Brussels": "BE",
    "Budapest": "HU", "Cologne": "DE", "Coesfeld": "DE", "Dresden": "DE",
    "Düsseldorf": "DE", "Erlangen": "DE", "Feistritz": "AT", "Frankfurt": "DE",
    "Geneva": "CH", "Hamburg": "DE", "Hannover": "DE", "Helsinki": "FI",
    "Innsbruck": "AT", "Istanbul": "TR", "Köln": "DE", "Leipzig": "DE",
    "Leuven": "BE", "Lisbon": "PT", "London": "GB", "Lucerne": "CH",
    "Madrid": "ES", "Malmö": "SE", "Milan": "IT", "Munich": "DE",
    "Neumarkt": "DE", "New York": "US", "Oslo": "NO", "Paris": "FR",
    "Prague": "CZ", "Riga": "LV", "Salzburg": "AT", "Stuttgart": "DE",
    "Tallin": "EE", "Tallinn": "EE", "Tirana": "AL", "Tokyo": "JP",
    "Vienna": "AT", "Warsaw": "PL", "Zagreb": "HR", "Zug": "CH",
}

DUTCH_CITIES = (
    "'s-Hertogenbosch", "s’Hertogenbosch", "Amsterdam", "Arnhem", "Breda",
    "Delft", "Den Bosch", "Den Haag", "Eindhoven", "Enschede", "Groningen",
    "Haarlem", "Heerlen", "Leiden", "Maastricht", "Middelburg", "Nijmegen",
    "Oss", "Rotterdam", "Rijssen", "The Hague", "Tilburg", "Utrecht", "Veere",
    "Wittem", "Zwolle",
)

KNOWN_VENUES = {
    "concertgebouw": "Concertgebouw", "muziekgebouw": "Muziekgebouw",
    "musiekgebouw": "Muziekgebouw", "tivolivredenburg": "TivoliVredenburg",
    "tivoli": "TivoliVredenburg", "stadsgehoorzaal": "Stadsgehoorzaal",
    "musis sacrum": "Musis Sacrum", "elbphilharmonie": "Elbphilharmonie",
    "tonhalle": "Tonhalle", "tonnhalle": "Tonhalle", "oosterpoort": "Oosterpoort",
    "noorderkerk": "Noorderkerk", "grotekerk": "Grote Kerk",
    "zeeuwse concertzaal": "Zeeuwse Concertzaal", "muziekcentrum": "Muziekcentrum",
    "philharmonie": "Philharmonie", "konzerthaus": "Konzerthaus",
    "konserthaus": "Konzerthaus", "schouwburg": "Schouwburg",
    "theater": "Theater", "theatre": "Theatre", "amare": "Amare",
}


def clean_text(node):
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def parse_time(text):
    match = re.search(r"\b(\d{1,2}:\d{2})\s*([ap]m)\b", text, re.I)
    if not match:
        return None
    return datetime.strptime(" ".join(match.groups()), "%I:%M %p").strftime("%H:%M")


def city_and_country(place):
    folded = place.casefold()
    country = next(
        (code for marker, code in COUNTRY_MARKERS.items() if re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", folded)),
        None,
    )

    candidates = sorted((*DUTCH_CITIES, *CITY_COUNTRIES), key=len, reverse=True)
    city = next((name for name in candidates if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", place, re.I)), None)
    if city == "s’Hertogenbosch":
        city = "'s-Hertogenbosch"
    elif city == "The Hague":
        city = "Den Haag"
    elif city == "Bilboa":
        city = "Bilbao"
    elif city == "Tallin":
        city = "Tallinn"

    if country is None and city:
        country = CITY_COUNTRIES.get(city, "NL")
    return city, country


def infer_venue(place, city, url):
    searchable = f"{place} {url}".casefold()
    for marker, venue in KNOWN_VENUES.items():
        if marker in searchable:
            return venue

    if not city:
        return None
    city_aliases = {city}
    if city == "'s-Hertogenbosch":
        city_aliases.add("s’Hertogenbosch")
    elif city == "Tallinn":
        city_aliases.add("Tallin")
    elif city == "Bilbao":
        city_aliases.add("Bilboa")
    remainder = place
    for alias in city_aliases:
        remainder = re.sub(re.escape(alias), "", remainder, count=1, flags=re.I)
    remainder = re.sub(r"\b(?:netherlands|nl|germany|austria|spain|switzerland|albania|estonia)\b", "", remainder, flags=re.I)
    remainder = remainder.strip(" ,-–/")
    # A bare city is not a venue, and artistic billing is not venue data.
    known_city_names = {name.casefold() for name in (*DUTCH_CITIES, *CITY_COUNTRIES)}
    invalid = (
        not remainder
        or "/" in remainder
        or len(remainder.split()) > 6
        or remainder.casefold() in known_city_names
        or re.fullmatch(r"[A-Z]{2}", remainder) is not None
        or re.search(r"\b(?:sinfonietta|camerata|orchestra|ensemble|festival)\b", remainder, re.I)
    )
    if invalid:
        return None
    return remainder


def title_for(article, place, venue_text, content):
    pieces = [venue_text, content]
    artistic_place = "/" in place or ("sinfonietta" in place.casefold())
    if artistic_place:
        pieces.insert(0, place)
    title = next((piece for piece in pieces if piece), "")
    title = re.sub(r"\bwww\.[^ ]+", "", title, flags=re.I)
    return title[:500].strip()


def parse_article(article):
    time_node = article.select_one("time[datetime]")
    if not time_node:
        return None
    raw_date = time_node.get("datetime", "").strip()
    try:
        event_date = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None

    place = clean_text(article.select_one(".event-place"))
    venue_text = clean_text(article.select_one(".event-venue"))
    content_node = article.select_one(".entry-content")
    content = clean_text(content_node)
    evidence = " ".join((place, venue_text, content)).strip()
    if re.search(r"\b(private|recording|recordings|cd recording|rehearsal)\b", evidence, re.I):
        return None

    city, country_code = city_and_country(place)
    links = [a.get("href") for a in article.select("a[href]")]
    url = next((urljoin(ARCHIVE_URL, link) for link in links if link), f"{ARCHIVE_URL}#{article.get('id', '')}")
    venue = infer_venue(place, city, url)
    title = title_for(article, place, venue_text, content)
    if not all((title, city, country_code, venue)):
        return None

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": parse_time(clean_text(time_node)),
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": evidence or None,
    }


class CandidaThompsonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="candidathompson_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching concert archive", event="crawler_url_fetch", url=ARCHIVE_URL)
        response = requests.get(ARCHIVE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []
        for article in soup.select("article.type-kilmo_event"):
            record = parse_article(article)
            if record:
                records.append(record)
        log_message(
            "Concert archive parsed",
            event="crawler_scrape_completed",
            url=ARCHIVE_URL,
            record_count=len(records),
        )
        return records


def main():
    CandidaThompsonCrawler().run()


if __name__ == "__main__":
    main()
