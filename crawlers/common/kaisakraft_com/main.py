import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.kaisakraft.com/"
SOURCE = "Kaisa Kraft"


# The calendar's location field is free text and generally names a venue rather
# than a postal address.  These fragments cover the venues in the published
# archive; an unknown location is deliberately skipped rather than assigned an
# unsafe home-city default.
CITY_FRAGMENTS = {
    "aalborg": "Aalborg",
    "alexandrianaukio": "Turku",
    "alavus": "Alavus",
    "angelniemen": "Salo",
    "aura": "Aura",
    "betel-kirkko": "Turku",
    "eurajoen": "Eurajoki",
    "gatorade areena": "Turku",
    "halikon": "Salo",
    "hannikais-sali": "Jyväskylä",
    "heikkilän sotilaskoti": "Turku",
    "helsinki": "Helsinki",
    "henrikin kirkko": "Turku",
    "henrikinkirkko": "Turku",
    "hirvensalon": "Turku",
    "hämeenlinna": "Hämeenlinna",
    "järvenpää": "Järvenpää",
    "kallion kirk": "Helsinki",
    "kaskenmäen": "Turku",
    "kirkkonummen": "Kirkkonummi",
    "konneves": "Konnevesi",
    "kosken kirkko": "Koski Tl",
    "laulutaiteen- ja tieteen keskus": "Helsinki",
    "lemin": "Lemi",
    "liedon": "Lieto",
    "lilla villan": "Sipoo",
    "littoisten kirkko": "Kaarina",
    "loimaan": "Loimaa",
    "maskun": "Masku",
    "merikarvian": "Merikarvia",
    "mikaelinkirkko": "Turku",
    "musiikkitalo": "Helsinki",
    "musikkens hus": "Aalborg",
    "nousiaisten": "Nousiainen",
    "paraisten": "Parainen",
    "perniön": "Salo",
    "puolalanpuisto": "Turku",
    "rovaniemen": "Rovaniemi",
    "runosmäen": "Turku",
    "salo": "Salo",
    "savonia-ammattikorkeakoulun": "Kuopio",
    "sibelius-muse": "Turku",
    "sigyn": "Turku",
    "siuntion": "Siuntio",
    "suurila": "Lieto",
    "säräpirtti": "Lemi",
    "taidekeskus salmela": "Mäntyharju",
    "tarvasjoen": "Lieto",
    "turun ": "Turku",
    "turku": "Turku",
    "uskela": "Salo",
    "ylösnousemuskappeli": "Turku",
    "ålborg": "Aalborg",
    "église st. pie x": "Luxembourg",
    "eglise de junglinster": "Junglinster",
}


def location_geography(location: str) -> tuple[str, str] | None:
    normalized = " ".join(location.lower().split())
    if normalized in {"online event", "helsinki, suomi", "turku, suomi"}:
        return None
    # Street-only entries are addresses, not defensible venue names.
    if normalized.startswith(("kirkkotie,", "seiskarinkatu,", "yliopistonkatu,")):
        return None

    country_code = "FI"
    if "luxemb" in normalized or "junglinster" in normalized:
        country_code = "LU"
    elif "tanska" in normalized or "aalborg" in normalized or "ålborg" in normalized:
        country_code = "DK"

    # Prefer an explicitly supplied city over a venue-name inference.
    explicit_cities = {
        "aalborg": "Aalborg", "alavus": "Alavus", "helsinki": "Helsinki",
        "hämeenlinna": "Hämeenlinna", "junglinster": "Junglinster",
        "kuopio": "Kuopio", "lemi": "Lemi", "lieto": "Lieto",
        "parainen": "Parainen", "salo": "Salo", "siuntio": "Siuntio",
        "sipoo": "Sipoo", "turku": "Turku",
    }
    parts = [part.strip().lower() for part in location.split(",")]
    for part in reversed(parts[1:]):
        if part in explicit_cities:
            return explicit_cities[part], country_code

    for fragment, city in CITY_FRAGMENTS.items():
        if fragment in normalized:
            return city, country_code
    return None


def parse_detail(session: requests.Session, url: str) -> dict:
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    event_data = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and value.get("@type") == "Event":
            event_data = value
            break

    time_from = None
    time_to = None
    if event_data:
        try:
            start = datetime.strptime(event_data["startDate"], "%m/%d/%Y %I:%M %p")
            time_from = start.strftime("%H:%M")
        except (KeyError, TypeError, ValueError):
            pass
        try:
            end = datetime.strptime(event_data["endDate"], "%m/%d/%Y %I:%M %p")
            time_to = end.strftime("%H:%M")
        except (KeyError, TypeError, ValueError):
            pass

    description_node = soup.select_one(".item-description")
    description = description_node.get_text("\n", strip=True) if description_node else None
    return {"time_from": time_from, "time_to": time_to, "description": description or None}


class KaisaKraftCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="kaisakraft_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        response = session.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []

        for row in soup.select(".events-container tbody tr"):
            cells = row.select("td")
            link = cells[0].find("a", href=True) if len(cells) >= 4 else None
            if not link:
                continue
            title = link.get_text(" ", strip=True)
            location = cells[3].get_text(" ", strip=True)
            url = urljoin(SOURCE_URL, link["href"])
            geography = location_geography(location)
            if not title or not location or not geography:
                log_message(
                    "Skipping event with unresolved venue or city",
                    event="crawler_record_skipped",
                    url=url,
                )
                continue
            try:
                event_date = datetime.strptime(cells[1].get_text(" ", strip=True), "%d %b %Y")
                date_value = event_date.strftime("%Y-%m-%d")
            except ValueError:
                log_message("Skipping event with invalid date", event="crawler_record_skipped", url=url)
                continue

            city, country_code = geography
            records.append({
                "title": title,
                "date": date_value,
                "url": url,
                "time_from": None,
                "time_to": None,
                "venue": location,
                "city": city,
                "country_code": country_code,
                "description": None,
            })

        def add_detail(record: dict) -> dict:
            try:
                detail = parse_detail(session, record["url"])
            except requests.RequestException as error:
                log_message(
                    "Concert detail fetch failed",
                    event="crawler_url_fetch_failed",
                    url=record["url"],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                return record
            return {**record, **detail}

        # Detail pages are independent, and the archive is long. A small pool
        # keeps the full-history scrape practical without burdening the site.
        with ThreadPoolExecutor(max_workers=6) as executor:
            records = list(executor.map(add_detail, records))
        return records


def main():
    KaisaKraftCrawler().run()


if __name__ == "__main__":
    main()
