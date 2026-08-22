import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Roberto Paternostro"
SOURCE_URL = "http://www.robertopaternostro.com/"
ARCHIVE_URL = urljoin(SOURCE_URL, "/de/aktuelles/")

MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "dezember": 12,
}

# Paternostro is a touring conductor.  The site does not attach structured
# geography to its posts, so only places explicitly named in an item are used.
PLACES = {
    "Aachen": "DE", "Bad Kissingen": "DE", "Bayreuth": "DE",
    "Berlin": "DE", "Bonn": "DE", "Dresden": "DE", "Essen": "DE",
    "Frankfurt": "DE", "Hamburg": "DE", "Kassel": "DE", "Köln": "DE",
    "Lübeck": "DE", "München": "DE", "Recklinghausen": "DE",
    "Stuttgart": "DE", "Wiesbaden": "DE", "Wuppertal": "DE",
    "Wien": "AT", "Vienna": "AT", "Grafenegg": "AT", "Salzburg": "AT",
    "Budapest": "HU", "Bukarest": "RO", "Bucharest": "RO",
    "Tel Aviv": "IL", "Jerusalem": "IL", "Haifa": "IL", "Eilat": "IL",
    "Toblach": "IT", "Dobbiaco": "IT", "San Remo": "IT", "Sanremo": "IT",
    "Prag": "CZ", "Prague": "CZ", "Bratislava": "SK", "Zagreb": "HR",
    "Sofia": "BG", "Belgrad": "RS", "Belgrade": "RS", "Cincinnati": "US",
    "Monte Carlo": "MC", "Monaco": "MC", "London": "GB", "Londoner": "GB",
}

VENUE_WORDS = re.compile(
    r"(?i)\b(akademie|auditorium|concert hall|konzerthaus|konzertsaal|kulturhaus|"
    r"musikhalle|musikverein|oper|opernhaus|opera|philharmonie|sala|saal|"
    r"staatsoper|theater|volksoper|festspielhaus|basilika|kirche|synagoge)\b"
)


def _clean(text):
    return re.sub(r"\s+", " ", text).strip()


def _get(url):
    log_message("Fetching page", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def _parse_dates(text):
    """Return every concrete German date in the item's body."""
    found = []
    pattern = re.compile(
        r"(?i)\b(\d{1,2})\.(?:\s*(?:/|und|,|-)?\s*(\d{1,2})\.)?\s*"
        r"(" + "|".join(MONTHS) + r")\s+(20\d{2})\b"
    )
    for match in pattern.finditer(text):
        first, second, month_name, year = match.groups()
        days = [int(first)] + ([int(second)] if second else [])
        for day in days:
            try:
                value = date(int(year), MONTHS[month_name.lower()], day).isoformat()
            except ValueError:
                continue
            if value not in found:
                found.append(value)

    numeric = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")
    for day, month, year in numeric.findall(text):
        try:
            value = date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
        if value not in found:
            found.append(value)
    return found


def _find_place(lines):
    for line in lines:
        for city, country_code in PLACES.items():
            if re.search(rf"(?<!\w){re.escape(city)}(?!\w)", line, re.I):
                return city.removesuffix("er"), country_code
    return None, None


def _find_venue(lines, city, title):
    joined = "\n".join([title, *lines])
    known_venues = (
        "Musikhalle Lübeck", "Liszt Akademie", "Konzertsaal der Lisztakademie",
        "Sala Radio", "Auditorium Grafenegg", "Bartok concert hall",
        "Theater an der Wien", "Tel-Aviv Museum of Art", "Recanati Auditorium",
        "Gustav Mahler Saal", "Staatsoper Prag", "Volksoper Wien",
    )
    for venue in known_venues:
        if venue.lower() in joined.lower():
            return venue

    for line in lines:
        if not VENUE_WORDS.search(line):
            continue
        # Reject lines which are clearly prose, repertoire, or performer lists.
        if len(line) > 70 or re.search(
            r"(?i)(https?://|\blink\b|\bweitere\b|\bjeweils\b|\bwird\b|"
            r"\baufführ|\bdirigent|\borchestra|\borchester|\bsymphonie|\bkonzert nr|"
            r"\bassociation|\bstrasse\b|\bstraße\b|\bA-\d)",
            line,
        ):
            continue
        venue = line
        if city and re.fullmatch(rf"(?i){re.escape(city)}", venue):
            continue
        if city and "," in venue and city.lower() in venue.lower():
            venue = venue.split(",", 1)[1].strip()
        if venue:
            return venue
    return None


def _parse_item(url):
    soup = _get(url)
    content = soup.select_one(".newscontent .inhalt, .leftBlock .inhalt")
    heading = soup.select_one(".newscontent h1, .leftBlock h1")
    if not content or not heading:
        return []

    lines = [_clean(line) for line in content.get_text("\n", strip=True).splitlines()]
    lines = [line for line in lines if line]
    description = "\n".join(lines)
    dates = _parse_dates(description)
    city, country_code = _find_place(lines)
    title = _clean(heading.get_text(" ", strip=True))
    venue = _find_venue(lines, city, title)
    if not dates or not city or not venue:
        return []

    time_match = re.search(r"(?i)\b(\d{1,2})[:.]([0-5]\d)\s*Uhr\b", description)
    time_from = f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else None
    return [{
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": time_from,
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    } for event_date in dates]


class RobertoPaternostroCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="robertopaternostro_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        soup = _get(ARCHIVE_URL)
        urls = []
        for link in soup.select("ul.liste_news a[href]"):
            url = urljoin(ARCHIVE_URL, link["href"])
            if url not in urls:
                urls.append(url)

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_parse_item, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Skipping unavailable detail page",
                        event="crawler_url_fetch_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        log_message("Parsed archive", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    RobertoPaternostroCrawler().run()


if __name__ == "__main__":
    main()
