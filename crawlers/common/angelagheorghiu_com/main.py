import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://angelagheorghiu.com/"
SOURCE = "Angela Gheorghiu"
API_URL = f"{SOURCE_URL}wp-admin/admin-ajax.php"

# The calendar is an international touring calendar. Locations are WordPress
# taxonomy labels rather than structured addresses, so resolve only cities for
# which the first-party title/venue text is unambiguous.
CITY_COUNTRIES = {
    "berlin": ("Berlin", "DE"),
    "bonn": ("Bonn", "DE"),
    "bordeaux": ("Bordeaux", "FR"),
    "brussels": ("Brussels", "BE"),
    "bruxelles": ("Brussels", "BE"),
    "bucharest": ("Bucharest", "RO"),
    "copenhagen": ("Copenhagen", "DK"),
    "dresden": ("Dresden", "DE"),
    "dublin": ("Dublin", "IE"),
    "geneva": ("Geneva", "CH"),
    "genève": ("Geneva", "CH"),
    "granada": ("Granada", "ES"),
    "klagenfurt": ("Klagenfurt", "AT"),
    "liége": ("Liège", "BE"),
    "liège": ("Liège", "BE"),
    "london": ("London", "GB"),
    "lucca": ("Lucca", "IT"),
    "monte carlo": ("Monte Carlo", "MC"),
    "nagoya": ("Nagoya", "JP"),
    "new york": ("New York", "US"),
    "ohrid": ("Ohrid", "MK"),
    "oradea": ("Oradea", "RO"),
    "otsu": ("Otsu", "JP"),
    "oxford": ("Oxford", "GB"),
    "palermo": ("Palermo", "IT"),
    "paris": ("Paris", "FR"),
    "prague": ("Prague", "CZ"),
    "rzeszow": ("Rzeszów", "PL"),
    "santa monica": ("Santa Monica", "US"),
    "seoul": ("Seoul", "KR"),
    "shanghai": ("Shanghai", "CN"),
    "shenzhen": ("Shenzhen", "CN"),
    "sofia": ("Sofia", "BG"),
    "tokyo": ("Tokyo", "JP"),
    "vienna": ("Vienna", "AT"),
    "weifang": ("Weifang", "CN"),
    "wuxi": ("Wuxi", "CN"),
    "xiamen": ("Xiamen", "CN"),
}

VENUE_BY_CITY = {
    "Dresden": "Semperoper Dresden",
    "Liège": "Opéra Royal de Wallonie-Liège",
}


def clean_text(value):
    if not value:
        return None
    text = BeautifulSoup(html.unescape(str(value)), "html.parser").get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text or None


def resolve_geography(title, venue, description):
    evidence = " ".join(filter(None, (title, venue, description))).casefold()
    # This listing is titled Osaka, but its own venue/excerpt identifies Biwako
    # Hall in Otsu; prefer the actual performance location.
    if "biwako" in evidence or "otsu" in evidence:
        return "Otsu", "JP"
    for marker, geography in CITY_COUNTRIES.items():
        if marker in evidence:
            return geography
    return None


def resolve_venue(raw_venue, city, description):
    venue = clean_text(raw_venue)
    if venue and venue.casefold().strip(" ,") != city.casefold():
        return venue
    if city in VENUE_BY_CITY:
        return VENUE_BY_CITY[city]
    # A short first-party excerpt consisting of a named hall/theatre/opera is
    # usable as venue evidence. Longer excerpts are programme descriptions.
    if description and len(description) <= 100 and re.search(
        r"\b(op[eé]ra|opera|theatre|theater|hall|auditorium|philharmoni|centre|center)\b",
        description,
        re.IGNORECASE,
    ):
        return description
    return None


class AngelaGheorghiuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="angelagheorghiu_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching calendar feed", event="crawler_url_fetch", url=API_URL)
        response = requests.post(
            API_URL,
            data={
                "action": "load_classes",
                "filters": "a:0:{}",
                "method": "0",
                "start": "0",
                "stop": "2147483647",
                "jumper": "0",
                "formatted": "m/d/y",
            },
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        events = response.json()

        records = []
        for event in events:
            try:
                title = clean_text(event.get("title"))
                description = clean_text(event.get("excerpt"))
                geography = resolve_geography(title, event.get("rooms"), description)
                if not title or not geography:
                    continue
                city, country_code = geography
                venue = resolve_venue(event.get("rooms"), city, description)
                url = event.get("single_href")
                date = datetime.strptime(
                    event["date"]["date_short"], "%d.%m.%Y"
                ).date().isoformat()
                time_from = event.get("date", {}).get("time") or None
                time_to = event.get("ending", {}).get("ending") or None
                if not venue or not url:
                    continue
                records.append(
                    {
                        "title": title,
                        "date": date,
                        "url": url,
                        "time_from": time_from,
                        "time_to": time_to,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": description,
                    }
                )
            except (KeyError, TypeError, ValueError) as error:
                log_message(
                    "Skipping malformed calendar event",
                    event="crawler_record_skipped",
                    url=event.get("single_href"),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        log_message(
            "Calendar feed parsed",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    AngelaGheorghiuCrawler().run()


if __name__ == "__main__":
    main()
