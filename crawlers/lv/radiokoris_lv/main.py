import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Latvijas Radio koris"
SOURCE_URL = "https://www.radiokoris.lv/lv/"
CONCERTS_URL = urljoin(SOURCE_URL, "koncerti/")

MONTHS = {
    "janvāris": 1, "februāris": 2, "marts": 3, "aprīlis": 4,
    "maijs": 5, "jūnijs": 6, "jūlijs": 7, "augusts": 8,
    "septembris": 9, "oktobris": 10, "novembris": 11, "decembris": 12,
}
COUNTRIES = {
    "latvija": "LV", "vācija": "DE", "beļģija": "BE", "austrālija": "AU",
    "nīderlande": "NL", "portugāle": "PT", "francija": "FR",
    "igaunija": "EE", "lietuva": "LT", "somija": "FI",
    "zviedrija": "SE", "norvēģija": "NO", "dānija": "DK", "šveice": "CH",
    "austrija": "AT", "itālija": "IT", "spānija": "ES", "polija": "PL",
    "apvienotā karaliste": "GB", "lielbritānija": "GB", "asv": "US",
}


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


def _clean(node):
    if not node:
        return None
    value = re.sub(r"\n\s*\n+", "\n", node.get_text("\n", strip=True)).strip()
    return value or None


def _year_for(day, month, title):
    numeric = re.search(rf"\b{day}[.]0?{month}[.](20\d{{2}})\b", title)
    if numeric:
        return int(numeric.group(1))
    today = date.today()
    year = today.year
    if date(year, month, day) < today:
        year += 1
    return year


def _location(raw_location):
    location = re.sub(r"\s+", " ", raw_location).strip(" ,|")
    normalized = location.casefold()

    country_code = "LV"
    for country_name, code in COUNTRIES.items():
        if country_name in normalized:
            country_code = code
            break

    # These first-party venue names unambiguously identify Riga even when the
    # page omits the city (the choir otherwise labels tours explicitly).
    riga_markers = (
        "rīgas ", "melngalvju nams", "dailes teātris",
        "sv. jāņa ev. luteriskā baznīca",
    )
    if any(marker in normalized for marker in riga_markers):
        return location.split(",")[0].strip(), "Rīga", "LV"

    parts = [part.strip() for part in re.split(r"\s*[|,]\s*", location) if part.strip()]
    if parts and parts[-1].casefold() in COUNTRIES:
        parts.pop()
    if len(parts) < 2:
        return None
    # A comma/pipe-separated first component is the venue and the following
    # component is the city. Reject tour summaries containing only city lists.
    venue, city = parts[0], parts[1]
    if venue.casefold() in {"rīga", "brisbena", "melburna", "pērta", "adelaida", "kanbera", "sidneja"}:
        return None
    return venue, city, country_code


def _detail(url):
    try:
        response = _session().get(url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        article = soup.select_one("article")
        title_node = article.select_one("h1") if article else None
        date_node = article.select_one(".event__dates li") if article else None
        location_node = article.select_one(".event__location") if article else None
        if not title_node or not date_node or not location_node:
            return None

        title = title_node.get_text(" ", strip=True)
        if "abonement" in title.casefold():
            return None
        match = re.search(
            r"(\d{1,2})[.]?\s+([a-zāčēģīķļņšūž]+)(?:,\s*(\d{1,2}):(\d{2}))?",
            date_node.get_text(" ", strip=True).casefold(),
        )
        if not match or match.group(2) not in MONTHS:
            return None

        located = _location(location_node.get_text(" ", strip=True))
        if not located:
            log_message("Skipping event with unresolved venue or city", event="crawler_record_skipped", url=url)
            return None

        body_nodes = article.select(".body-copy")
        description = "\n".join(filter(None, (_clean(node) for node in body_nodes))) or None
        day, month = int(match.group(1)), MONTHS[match.group(2)]
        year = _year_for(day, month, title)
        time_from = None
        if match.group(3):
            time_from = f"{int(match.group(3)):02d}:{match.group(4)}"
            if time_from == "00:00":
                time_from = None
        venue, city, country_code = located
        base_record = {
            "title": title,
            "url": url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }
        # Some production pages represent a short run with one detail page.
        # The title and body explicitly enumerate consecutive performances;
        # later performances have no published start time, so retain None.
        end_day = day
        range_match = re.search(
            rf"\b{day}[.]?\s*-\s*(\d{{1,2}})[.]?\s+{re.escape(match.group(2))}\b",
            title.casefold(),
        )
        if range_match:
            end_day = int(range_match.group(1))
        records = []
        for occurrence_day in range(day, end_day + 1):
            record = dict(base_record)
            record["date"] = date(year, month, occurrence_day).isoformat()
            if occurrence_day != day:
                record["time_from"] = None
            records.append(record)
        return records
    except (requests.RequestException, ValueError, KeyError) as error:
        log_message(
            "Skipping event after detail fetch failed",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


def _event_urls():
    session = _session()
    urls = []
    seen_pages = set()
    page = 0
    while True:
        response = session.get(CONCERTS_URL, params={"p": page}, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        page_urls = [
            urljoin(CONCERTS_URL, anchor["href"])
            for anchor in soup.select('figure h3 a[href*="/koncerti/notikums/"]')
        ]
        signature = tuple(page_urls)
        if not page_urls or signature in seen_pages:
            break
        seen_pages.add(signature)
        urls.extend(page_urls)
        next_page = soup.select_one(f'a[href="?p={page + 1}"]')
        if not next_page:
            break
        page += 1
    return list(dict.fromkeys(urls))


class RadiokorisCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="radiokoris_lv",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="LV",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(_detail, url) for url in _event_urls()]
            for future in as_completed(futures):
                detail_records = future.result()
                if detail_records:
                    records.extend(detail_records)
        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["url"]))
        return records


def main():
    RadiokorisCrawler().run()


if __name__ == "__main__":
    main()
