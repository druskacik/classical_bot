import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Kodály Filharmónia Debrecen"
SOURCE_URL = "https://kodalyfilharmonikusok.hu/"
CALENDAR_API = urljoin(SOURCE_URL, "events/getEventsForCalendarAlt")

# Category 1 is the site's first-party umbrella for the organisation's concert
# programme. It includes orchestra, choir, family/youth and guest concerts, but
# excludes the separately identified exhibition category (7).
CONCERT_CATEGORY = "1"
FIRST_ARCHIVE_YEAR = 2016
MONTHS = {
    "január": 1,
    "február": 2,
    "március": 3,
    "április": 4,
    "május": 5,
    "június": 6,
    "július": 7,
    "augusztus": 8,
    "szeptember": 9,
    "október": 10,
    "november": 11,
    "december": 12,
}
CITY_HINTS = {
    "balmazújváros": ("Balmazújváros", "HU"),
    "békéscsaba": ("Békéscsaba", "HU"),
    "berettyóújfalu": ("Berettyóújfalu", "HU"),
    "budapest": ("Budapest", "HU"),
    "debrecen": ("Debrecen", "HU"),
    "hajdúböszörmény": ("Hajdúböszörmény", "HU"),
    "hajdúszoboszló": ("Hajdúszoboszló", "HU"),
    "kapolcs": ("Kapolcs", "HU"),
    "körösladány": ("Körösladány", "HU"),
    "miskolc": ("Miskolc", "HU"),
    "nyíregyháza": ("Nyíregyháza", "HU"),
    "pécs": ("Pécs", "HU"),
    "püspökladány": ("Püspökladány", "HU"),
    "sopron": ("Sopron", "HU"),
    "székesfehérvár": ("Székesfehérvár", "HU"),
    "szentendre": ("Szentendre", "HU"),
    "tatabánya": ("Tatabánya", "HU"),
    "tiszafüred": ("Tiszafüred", "HU"),
    "tokaj": ("Tokaj", "HU"),
    "marosvásárhely": ("Marosvásárhely", "RO"),
    "nagyvárad": ("Nagyvárad", "RO"),
    "sepsiszentgyörgy": ("Sepsiszentgyörgy", "RO"),
    "székelyudvarhely": ("Székelyudvarhely", "RO"),
}


def _text(node):
    return node.get_text("\n", strip=True) if node else ""


def _parse_datetime(value):
    match = re.search(
        r"(\d{4})\.\s*([a-záéíóöőúüű]+)\s+(\d{1,2})\.,?\s*(\d{1,2})[.:](\d{2})",
        value.casefold(),
    )
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        event_date = date(
            int(match.group(1)), MONTHS[match.group(2)], int(match.group(3))
        )
    except ValueError:
        return None
    return event_date.isoformat(), f"{int(match.group(4)):02d}:{match.group(5)}"


def _city_and_country(venue, address):
    combined = f"{venue} {address}".casefold()
    for hint, result in CITY_HINTS.items():
        if hint in combined:
            return result

    # Hungarian addresses consistently begin with a four-digit postcode.
    match = re.search(r"\b\d{4}\s+([^,\n]+)", address)
    if match:
        city = re.sub(r"\s+(?:utca|út|tér|köz)\b.*", "", match.group(1), flags=re.I).strip()
        if city:
            return city, "HU"

    # Most entries without an address are events in the institution's home
    # city. Do not apply that default when the venue text signals a tour.
    if not any(marker in combined for marker in (" - ", ", románia", "fesztivál,")):
        return "Debrecen", "HU"
    return None


def _detail_url(calendar_url):
    path = urlparse(calendar_url).path
    match = re.search(r"/(?:hangversenynaptar/elonezet|events/preview)/(\d+)/([^/?#]+)", path)
    if not match:
        return None
    return urljoin(SOURCE_URL, f"hangversenynaptar/esemeny/{match.group(1)}/{match.group(2)}")


class KodalyFilharmoniaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="kodalyfilharmonikusok_hu",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="HU",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "classical-crawler/1.0"})

    def _calendar_urls(self):
        urls = []
        for year in range(FIRST_ARCHIVE_YEAR, date.today().year + 3):
            page = 1
            while True:
                response = self.session.post(
                    CALENDAR_API,
                    data={"page": page, "year": year, "category[]": CONCERT_CATEGORY},
                    headers={"X-Requested-With": "XMLHttpRequest"},
                    timeout=90,
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("result"):
                    break
                soup = BeautifulSoup(payload.get("view", ""), "html.parser")
                for anchor in soup.select('.event .title a[href]'):
                    detail_url = _detail_url(urljoin(SOURCE_URL, anchor["href"]))
                    if detail_url:
                        urls.append(detail_url)
                max_page = int(payload.get("max_page") or 1)
                if page >= max_page:
                    break
                page += 1
        return list(dict.fromkeys(urls))

    def _scrape_detail(self, url):
        response = self.session.get(url, timeout=90)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        article = soup.select_one("article.article_container.event_data")
        if not article:
            return None

        title = _text(article.select_one(".event_data_left h3"))
        parsed_datetime = _parse_datetime(_text(article.select_one(".time")))
        location = article.select_one(".location")
        venue = _text(location.select_one(".location_name")) if location else ""
        address = _text(location.select_one(".location_address")) if location else ""
        geography = _city_and_country(venue, address)
        if not title or not parsed_datetime or not venue or not geography:
            log_message(
                "Skipping event with incomplete required fields",
                event="crawler_record_skipped",
                level="warning",
                url=url,
            )
            return None

        description_parts = [
            _text(article.select_one(".lead")),
            _text(article.select_one(".event_data_bottom")),
        ]
        description = "\n\n".join(part for part in description_parts if part) or None
        event_date, time_from = parsed_datetime
        city, country_code = geography
        return {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }

    def scrape(self):
        log_message("Fetching concert archive", event="crawler_url_fetch", url=CALENDAR_API)
        urls = self._calendar_urls()
        records = []
        for url in urls:
            try:
                record = self._scrape_detail(url)
            except requests.RequestException as error:
                log_message(
                    "Concert detail request failed",
                    event="crawler_url_fetch_failed",
                    level="warning",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
        log_message(
            "Concert archive parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    KodalyFilharmoniaCrawler().run()


if __name__ == "__main__":
    main()
