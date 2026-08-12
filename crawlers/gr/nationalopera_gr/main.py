import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.nationalopera.gr/"
SOURCE = "Greek National Opera"
CALENDAR_URL = urljoin(SOURCE_URL, "component/k2pro/")
FIRST_ARCHIVE_YEAR = 2008

MONTHS = {
    "ιαν": 1, "φεβ": 2, "μαρ": 3, "απρ": 4, "μαϊ": 5, "μαι": 5,
    "ιουν": 6, "ιουλ": 7, "αυγ": 8, "σεπ": 9, "οκτ": 10,
    "νοε": 11, "δεκ": 12,
}

HOME_VENUES = {
    "/aithousa-stavros-niarxos/": ("Αίθουσα Σταύρος Νιάρχος", "Αθήνα"),
    "/enalaktiki-skini/": ("Εναλλακτική Σκηνή Εθνικής Λυρικής Σκηνής", "Αθήνα"),
}


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _month_number(text):
    folded = text.casefold().replace("ά", "α").replace("έ", "ε").replace("ή", "η").replace("ί", "ι").replace("ϊ", "ι").replace("ΐ", "ι").replace("ό", "ο").replace("ύ", "υ").replace("ώ", "ω")
    return next((number for prefix, number in MONTHS.items() if folded.startswith(prefix)), None)


def _tour_location(soup, event_date):
    container = soup.select_one(".cf_parprotagon")
    if not container:
        return None
    for line in container.get_text("\n", strip=True).splitlines():
        match = re.search(
            r"\b(\d{1,2})\s+([Α-ΩΆΈΉΊΌΎΏΪΫα-ωάέήίόύώϊϋΐΰ]+)(?:\s+\d{4})?\s*\|\s*"
            r"(\d{1,2})[.:](\d{2})\s*[–-]\s*([^,–]+),\s*(.+)$",
            _clean(line),
        )
        if not match:
            continue
        month = _month_number(match.group(2))
        if month == event_date.month and int(match.group(1)) == event_date.day:
            return {
                "time_from": f"{int(match.group(3)):02d}:{match.group(4)}",
                "city": _clean(match.group(5)),
                "venue": _clean(match.group(6)),
            }
    return None


class NationalOperaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nationalopera_gr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GR",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self, session=None, first_year=FIRST_ARCHIVE_YEAR, last_year=None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "classical-bot/1.0"})
        self.first_year = first_year
        self.last_year = last_year or date.today().year + 2

    def _get_soup(self, url, **params):
        log_message("Fetching National Opera page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, params=params or None, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def _occurrences(self):
        occurrences = []
        for year in range(self.first_year, self.last_year + 1):
            for month in range(1, 13):
                soup = self._get_soup(
                    CALENDAR_URL,
                    view="calendar", task="ajax", month=month, year=year,
                    display="full", Itemid=213,
                )
                for cell in soup.select("td.calendarDateLinked"):
                    day_link = cell.select_one('a.day[href*="from="]')
                    if not day_link:
                        continue
                    match = re.search(r"from=(\d{4}-\d{2}-\d{2})", day_link.get("href", ""))
                    if not match:
                        continue
                    try:
                        event_date = date.fromisoformat(match.group(1))
                    except ValueError:
                        continue
                    for link in cell.select("a.dayItem"):
                        url = urljoin(SOURCE_URL, link.get("href", ""))
                        if "/item/" in url:
                            occurrences.append((event_date, url, _clean(link.get_text(" ", strip=True))))
        return occurrences

    def scrape(self):
        occurrences = self._occurrences()
        details = {}
        records = []
        for event_date, url, calendar_title in occurrences:
            # Detail HTML is cached locally per scrape while occurrence-specific
            # location parsing remains tied to its exact calendar date.
            if url not in details:
                details[url] = self._get_soup(url)
            soup = details[url]
            title_node = soup.select_one("h1, .itemTitle")
            title = _clean(title_node.get_text(" ", strip=True) if title_node else calendar_title) or calendar_title
            location = _tour_location(soup, event_date)
            if not location:
                location = next(
                    ({"venue": venue, "city": city, "time_from": None}
                     for path, (venue, city) in HOME_VENUES.items() if path in url), None,
                )
            if not location or not location["venue"] or not location["city"]:
                log_message("Skipping event without defensible location", event="crawler_event_skipped", url=url)
                continue
            parts = []
            for selector in (".itemBody", ".cf_dimomada", ".cf_parprotagon"):
                node = soup.select_one(selector)
                text = _clean(node.get_text("\n", strip=True)) if node else ""
                if text and text not in parts:
                    parts.append(text)
            records.append({
                "title": title, "date": event_date.isoformat(), "url": url,
                "time_from": location.get("time_from"), "venue": location["venue"],
                "city": location["city"], "description": "\n\n".join(parts) or None,
            })
        log_message("National Opera scrape completed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    NationalOperaCrawler().run()


if __name__ == "__main__":
    main()
