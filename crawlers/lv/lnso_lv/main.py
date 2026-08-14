import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Latvijas Nacionālais simfoniskais orķestris"
SOURCE_URL = "https://www.lnso.lv/"
API_URL = f"{SOURCE_URL}wp-json/wp/v2"


def _session():
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "classical-concert-crawler/1.0"
    return session


def _get_collection(endpoint):
    records = []
    page = 1
    session = _session()
    while True:
        params = (
            {
                "per_page": 100,
                "page": page,
                "orderby": "date",
                "order": "asc",
                "_fields": "id,date,link,title,hall",
            }
            if endpoint == "events"
            else {"per_page": 100, "page": page, "_fields": "id,name"}
        )
        response = session.get(
            f"{API_URL}/{endpoint}",
            params=params,
            timeout=60,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        records.extend(batch)
        if page >= int(response.headers.get("X-WP-TotalPages", "1")):
            break
        page += 1
    return records


def _location(venue):
    """Resolve only places for which the first-party hall name is conclusive."""
    normalized = venue.casefold()
    foreign_places = (
        (("beauvoir-en-royans", "bovuāranruajān"), "Beauvoir-en-Royans", "FR"),
        (("metz",), "Metz", "FR"),
        (("aix-en-provence",), "Aix-en-Provence", "FR"),
        (("senrobēra", "saint-robert", "chaise-dieu", "šezdjē"), "La Chaise-Dieu", "FR"),
        (("paris",), "Paris", "FR"),
        (("tallin", "estonia"), "Tallinn", "EE"),
        (("vilnius",), "Vilnius", "LT"),
    )
    for markers, city, country_code in foreign_places:
        if any(marker in normalized for marker in markers):
            return city, country_code

    latvian_places = (
        (("rēzekne", "gors"), "Rēzekne"),
        (("cēsis",), "Cēsis"),
        (("ventspils",), "Ventspils"),
        (("liepāja", "lielais dzintars"), "Liepāja"),
        (("jūrmala", "jurmala", "dzintaru"), "Jūrmala"),
        (("rīga", "riga", "lielā ģilde", "kongresu nams", "hanzas perons", "hanzas iela",
          "mūzikas nams daile", "dailes teātris", "lnso, vsia", "amatu iela",
          "rīgas doms", "rīgas sv", "latvijas nacionālā opera", "aspazijas bulvāris",
          "kalnciema iela", "latviešu biedrības", "miera iela"), "Rīga"),
    )
    for markers, city in latvian_places:
        if any(marker in normalized for marker in markers):
            return city, "LV"
    return None


def _detail(event, venue):
    url = event["link"]
    try:
        response = _session().get(url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        body = soup.select_one(".single-event__lead")
        description = None
        if body:
            description = re.sub(r"\n\s*\n+", "\n", body.get_text("\n", strip=True)) or None

        # The rendered page is authoritative when its venue was edited after
        # the REST taxonomy assignment.
        venue_node = soup.select_one(".single-event__venue")
        rendered_venue = venue_node.get_text(" ", strip=True) if venue_node else venue
        location = _location(rendered_venue)
        if not location:
            log_message("Skipping event with unresolved location", event="crawler_record_skipped", url=url)
            return None

        start = datetime.fromisoformat(event["date"])
        city, country_code = location
        return {
            "title": html.unescape(event["title"]["rendered"]).strip(),
            "date": start.date().isoformat(),
            "url": url,
            "time_from": start.time().isoformat(timespec="minutes"),
            "time_to": None,
            "venue": rendered_venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }
    except (requests.RequestException, ValueError, KeyError) as error:
        log_message(
            "Skipping event after detail fetch failed",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


class LnsoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="lnso_lv",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="LV",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        halls = {item["id"]: html.unescape(item["name"]) for item in _get_collection("hall")}
        all_events = _get_collection("events")
        # Legacy records predating the Latvian archive use /en/ URLs despite
        # containing Latvian titles and pages. Once Latvian URLs begin, prefer
        # them so translated copies of the same performance are not emitted.
        latvian_dates = [item["date"] for item in all_events if "/en/" not in item["link"]]
        first_latvian_date = min(latvian_dates) if latvian_dates else None
        events = [
            item
            for item in all_events
            if "/en/" not in item["link"]
            or (first_latvian_date is not None and item["date"] < first_latvian_date)
        ]

        candidates = []
        for event in events:
            hall_ids = event.get("hall") or []
            venue = halls.get(hall_ids[0]) if hall_ids else None
            if not venue or not _location(venue):
                log_message(
                    "Skipping event without a resolvable hall",
                    event="crawler_record_skipped",
                    url=event.get("link"),
                )
                continue
            candidates.append((event, venue))

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_detail, event, venue) for event, venue in candidates]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)
        records.sort(key=lambda item: (item["date"], item["time_from"], item["url"]))
        return records


def main():
    LnsoCrawler().run()


if __name__ == "__main__":
    main()
