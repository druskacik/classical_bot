import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.alexpotter.info/"
SOURCE = "Alex Potter"

# The diary is an international touring schedule and does not expose structured
# geography. These are cities used by the source, including common translations.
CITY_COUNTRIES = {
    "Alkmaar": "NL", "Amsterdam": "NL", "Antwerp": "BE", "Asciano": "IT",
    "Barcelona": "ES", "Basel": "CH", "Beaune": "FR", "Berlin": "DE",
    "Bern": "CH", "Blaibach": "DE", "Boutersem": "BE", "Bremen": "DE",
    "Bruges": "BE", "Brussels": "BE", "Bulle": "CH", "Daejeon": "KR",
    "Den Haag": "NL", "Dresden": "DE", "Eindhoven": "NL", "Freiburg": "DE",
    "Gdańsk": "PL", "Geneva": "CH", "Genève": "CH", "Gent": "BE",
    "Haarlem": "NL", "Hamburg": "DE", "Heerlen": "NL", "Hildesheim": "DE",
    "Incheon": "KR", "Klagenfurt": "AT", "Köln": "DE", "La Chaux-de-Fonds": "CH",
    "Le Sentier": "CH", "Leipzig": "DE", "Ljubljana": "SI", "Ludwigsburg": "DE",
    "Luxembourg": "LU", "Madrid": "ES", "München": "DE", "Naarden": "NL",
    "Nijmegen": "NL", "Northeim": "DE", "Oberfrick": "CH", "Paris": "FR",
    "Prague": "CZ", "Reykjavik": "IS", "Rhenen": "NL", "Roggenburg": "DE",
    "Saessolsheim": "FR", "Saint-Maurice": "CH", "Seoul": "KR", "St. Gallen": "CH",
    "Stuttgart": "DE", "Tilburg": "NL", "Trogen": "CH", "Uetersen": "DE",
    "Utrecht": "NL", "Vienna": "AT", "Weilburg": "DE", "Zürich": "CH",
}


def clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip()


def parse_location(raw_location):
    location = clean_text(raw_location)
    if not location:
        return None
    location = re.sub(r"^https?://", "", location, flags=re.I)

    special = {
        "Centre de Congressos, Andorra": ("Andorra la Vella", "Centre de Congressos", "AD"),
        "Kloster Roggenburg": ("Roggenburg", "Kloster Roggenburg", "DE"),
        "Abbaye - Saint-Michel en Thiérache (France)": (
            "Saint-Michel en Thiérache", "Abbaye de Saint-Michel", "FR"
        ),
        "Wiener Konzerthaus": ("Vienna", "Wiener Konzerthaus", "AT"),
    }
    if location in special:
        return special[location]

    # Longest match first prevents Geneva-style substrings from winning over a
    # more precise location name such as La Chaux-de-Fonds.
    match = next(
        (city for city in sorted(CITY_COUNTRIES, key=len, reverse=True)
         if re.search(rf"(?<!\w){re.escape(city)}(?!\w)", location, re.I)),
        None,
    )
    if not match:
        return None

    city = "Geneva" if match in {"Geneva", "Genève"} else match
    venue = re.sub(rf"(?<!\w){re.escape(match)}(?!\w)", " ", location, flags=re.I)
    venue = re.sub(
        r"\((?:CH|F|Germany|Switzerland|The Netherlands|Belgium|Korea|Poland|Italy|France|Austria|Slovenia)\)",
        " ", venue, flags=re.I,
    )
    venue = re.sub(r"\b(?:Germany|Switzerland|The Netherlands|Belgium|Korea|Poland|Italy|France|Austria|Slovenia)\b", " ", venue, flags=re.I)
    venue = re.sub(r"^[\s,–.\-]+|[\s,–.\-]+$", "", venue)
    venue = clean_text(venue)
    if not venue:
        return None
    return city, venue, CITY_COUNTRIES[match]


class AlexPotterCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="alexpotter_info",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching concert diary", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []

        for card in soup.select(".cal_item"):
            title = clean_text(getattr(card.select_one(".cal_title--fs3"), "get_text", lambda: None)())
            composer = clean_text(getattr(card.select_one(".cal_composer--fs4"), "get_text", lambda: None)())
            ensemble_node = card.select_one(".cal_ensemble--fs5.w-richtext")
            ensemble = clean_text(ensemble_node.get_text(" ") if ensemble_node else None)
            location_node = card.select_one(".cal_venue--fs4")
            parsed_location = parse_location(location_node.get_text(" ") if location_node else None)
            date_parts = [clean_text(node.get_text()) for node in card.select(".cal_date_big--fs3, .cal_date_small--fs4")]

            # A recording is not a public occurrence. Masterclasses are included
            # only when the source explicitly advertises a concert.
            lowered_title = (title or "").lower()
            if "recording" in lowered_title or ("masterclass" in lowered_title and "concert" not in lowered_title):
                continue
            if not title or not parsed_location or len(date_parts) != 3:
                continue
            try:
                event_date = datetime.strptime(f"{date_parts[1]} {date_parts[2]}", "%d %b %Y").date().isoformat()
            except ValueError:
                continue

            link = card.select_one("a.cal_buttom[href]")
            event_url = urljoin(SOURCE_URL, link.get("href")) if link else SOURCE_URL
            city, venue, country_code = parsed_location
            description = "\n".join(part for part in (composer, title, ensemble) if part)
            records.append({
                "title": title,
                "date": event_date,
                "url": event_url,
                "time_from": None,
                "time_to": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description or None,
            })

        log_message("Concert diary parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    AlexPotterCrawler().run()


if __name__ == "__main__":
    main()
