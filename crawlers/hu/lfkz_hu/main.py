import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://lfkz.hu/"
CALENDAR_URL = urljoin(SOURCE_URL, "hu/koncertnaptar")
SOURCE = "Liszt Ferenc Kamarazenekar"
HUNGARIAN_MONTHS = {
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

# Older entries often name only a well-known venue. These are stable, specific
# venue-to-city relationships, not defaults for the touring orchestra.
VENUE_CITIES = {
    "angyalföldi józsef attila művelődési központ": "Budapest",
    "bmc": "Budapest",
    "budapest music center": "Budapest",
    "budapest music center könyvtára": "Budapest",
    "budapest-belvárosi nagyboldogasszony főplébánia-templom": "Budapest",
    "budapesti operettszínház": "Budapest",
    "csabagyöngye kulturális központ": "Békéscsaba",
    "deák téri evangélikus templom": "Budapest",
    "festetics palota": "Budapest",
    "gárdonyi géza színház": "Eger",
    "gödöllői királyi kastély": "Gödöllő",
    "hírös agóra kulturális és ifjúsági központ": "Kecskemét",
    "kálmán imre kulturális központ": "Siófok",
    "kaposvári református templom": "Kaposvár",
    "kölcsey központ": "Debrecen",
    "magyar zene háza": "Budapest",
    "müha művészetek háza miskolc": "Miskolc",
    "müpa": "Budapest",
    "müpa fesztivál színház": "Budapest",
    "müpa bartók béla hangversenyterem": "Budapest",
    "müpa — bartók béla nemzeti hangversenyterem": "Budapest",
    "müpa- bartók béla nemzeti hangversenyterem": "Budapest",
    "óbudai társaskör": "Budapest",
    "óbudai társaskör kertje": "Budapest",
    "pesti vigadó": "Budapest",
    "pomázi művelődési ház és könyvtár": "Pomáz",
    "pozsonyi úti református templom": "Budapest",
    "rumbach zsinagóga": "Budapest",
    "szegedi nemzeti színház": "Szeged",
    "szent istván bazilika": "Budapest",
    "szivárvány kultúrpalota": "Kaposvár",
    "vörösmarty színház": "Székesfehérvár",
    "zeneakadémia": "Budapest",
}

COUNTRY_NAMES = {
    "ausztria", "brazília", "csehország", "franciaország", "grúzia",
    "horvátország", "kolumbia", "lengyelország", "németország", "olaszország",
    "oroszország", "románia", "spanyolország", "svájc", "szerbia", "szlovákia",
    "szlovénia", "törökország",
}


def clean_text(value):
    if value is None:
        return None
    text = re.sub(r"\s+", " ", value.get_text(" ", strip=True) if hasattr(value, "get_text") else str(value)).strip()
    return text or None


def parse_date(value):
    match = re.search(r"(\d{4})\.\s*([a-záéíóöőúüű]+)\s+(\d{1,2})\.?", value.lower())
    if not match or match.group(2) not in HUNGARIAN_MONTHS:
        return None
    try:
        return datetime(
            int(match.group(1)), HUNGARIAN_MONTHS[match.group(2)], int(match.group(3))
        ).date().isoformat()
    except ValueError:
        return None


def parse_location(value):
    value = clean_text(value)
    if not value:
        return None, None

    # Recent listings explicitly use "City, Venue". Historical Budapest
    # listings frequently use "Venue, Hall", handled by the venue map first.
    normalized = re.sub(r"\s*-\s*MINDEN JEGY ELKELT!.*$", "", value, flags=re.I).strip()
    lower = normalized.casefold()
    for venue, city in sorted(VENUE_CITIES.items(), key=lambda item: len(item[0]), reverse=True):
        if lower == venue or lower.startswith(venue + ","):
            return city, normalized

    comma_parts = [part.strip() for part in normalized.split(",")]
    if len(comma_parts) >= 3 and comma_parts[-1].casefold() in COUNTRY_NAMES:
        # International tour entries use "Venue, City, Country".
        return comma_parts[-2], ", ".join(comma_parts[:-2])

    if "," in normalized:
        city, venue = (part.strip() for part in normalized.split(",", 1))
        if city and venue:
            return city, venue

    # A few entries use the equally explicit "City - Venue" spelling.
    match = re.match(r"^([^–—-]+?)\s+[-–—]\s+(.+)$", normalized)
    if match:
        city, venue = (part.strip() for part in match.groups())
        if city and venue:
            return city, venue

    city = VENUE_CITIES.get(lower)
    return (city, normalized) if city else (None, None)


class LfkzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="lfkz_hu",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="HU",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "classical-crawler/1.0 (+https://classicalbot.com)"

    def _get_soup(self, url, params=None):
        log_message("Fetching crawler page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.content, "html.parser")

    def _discover_years(self):
        soup = self._get_soup(CALENDAR_URL)
        years = []
        for option in soup.select('select[name="search_year"] option[value]'):
            value = option.get("value", "")
            if value.isdigit():
                years.append(int(value))
        if not years:
            raise ValueError("Concert calendar did not expose any archive years")
        return sorted(set(years))

    def _summaries_for_year(self, year):
        summaries = []
        page = 1
        while True:
            soup = self._get_soup(CALENDAR_URL, {"search_year": year, "page": page})
            cards = soup.select(".calendar-event-item")
            if not cards:
                break
            for card in cards:
                link = card.select_one('a[href*="/hu/koncert/"]')
                meta = clean_text(card.select_one(".calendar-event-meta"))
                title = clean_text(card.select_one(".calendar-event-title"))
                if not link or not meta or not title:
                    continue
                parts = [part.strip() for part in meta.split("|")]
                if len(parts) < 3:
                    continue
                city, venue = parse_location(" | ".join(parts[2:]))
                event_date = parse_date(parts[0])
                time_match = re.fullmatch(r"\d{1,2}:\d{2}", parts[1])
                if not event_date or not city or not venue:
                    continue
                summaries.append(
                    {
                        "title": title,
                        "date": event_date,
                        "time_from": parts[1].zfill(5) if time_match and parts[1] != "00:00" else None,
                        "time_to": None,
                        "url": urljoin(SOURCE_URL, link["href"]),
                        "venue": venue,
                        "city": city,
                    }
                )

            next_link = soup.select_one(f'.pagination a[href*="page={page + 1}"]')
            if not next_link:
                break
            page += 1
        return summaries

    def _description(self, url):
        soup = self._get_soup(url)
        sections = []
        for selector in (
            ".calendar-event-subtitle",
            ".calendar-event-participants",
            ".calendar-event-description",
        ):
            text = clean_text(soup.select_one(selector))
            if text and text not in sections:
                sections.append(text)
        return "\n".join(sections) or None

    def scrape(self):
        summaries = []
        for year in self._discover_years():
            summaries.extend(self._summaries_for_year(year))

        records = []
        seen = set()
        for summary in summaries:
            if summary["url"] in seen:
                continue
            seen.add(summary["url"])
            try:
                summary["description"] = self._description(summary["url"])
            except requests.RequestException as error:
                log_message(
                    "Concert detail fetch failed",
                    event="crawler_detail_fetch_failed",
                    url=summary["url"],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                summary["description"] = None
            records.append(summary)
        return records


def main():
    LfkzCrawler().run()


if __name__ == "__main__":
    main()
