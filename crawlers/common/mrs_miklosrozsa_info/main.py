import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "The Miklós Rózsa Society"
SOURCE_URL = "https://mrs.miklosrozsa.info/"
PERFORMANCES_URL = "https://www.miklosrozsa.info/html/performances.html"
ARCHIVE_URL = "https://www.miklosrozsa.info/html/PastConcertArchive.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "set": 9, "oct": 10, "october": 10, "nov": 11,
    "november": 11, "dec": 12, "december": 12,
}
MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_RE = re.compile(
    rf"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday,?\s+)?"
    rf"(?P<month>{MONTH_PATTERN})\.?\s+"
    rf"(?P<days>\d{{1,2}}(?:st|nd|rd|th)?(?:\s*(?:-|/|,|&|and)\s*\d{{1,2}}(?:st|nd|rd|th)?)*)"
    rf"(?:,)?\s+(?P<year>20\d{{2}})",
    re.IGNORECASE,
)

COUNTRIES = {
    "united states of america": "US", "united states": "US", "u.s.a.": "US",
    "usa": "US", "u.s.": "US", "uk": "GB", "u.k.": "GB",
    "england": "GB", "wales": "GB", "scotland": "GB", "spain": "ES",
    "germany": "DE", "hungary": "HU", "canada": "CA", "france": "FR",
    "italy": "IT", "switzerland": "CH", "austria": "AT", "sweden": "SE",
    "denmark": "DK", "finland": "FI", "norway": "NO", "netherlands": "NL",
    "belgium": "BE", "poland": "PL", "romania": "RO", "czech republic": "CZ",
    "czech rep": "CZ", "north macedonia": "MK", "australia": "AU",
    "japan": "JP", "taiwan": "TW", "argentina": "AR", "brazil": "BR",
    "portugal": "PT", "ireland": "IE", "lithuania": "LT", "qatar": "QA",
    "south korea": "KR", "philippines": "PH",
}

# The page is free-form prose. These names provide conservative geography for
# entries where a country and a real performance venue are both present.
CITY_NAMES = [
    "Stratford", "Highland Heights", "Venice", "Vero Beach", "Stuart", "Oakland",
    "Johnstown", "Nottingham", "Sheffield", "Manchester", "Long Beach", "Ondara",
    "Edinburgh", "Pécs", "Ingelheim", "Gelsenkirchen", "Recklinghausen", "Kamen",
    "Wesel", "Herne", "Leicester", "Palo Alto", "Zaragoza", "Budapest", "Reutlingen",
    "Huntsville", "Munich", "Bilbao", "Vienna", "Târgu Mureș", "Bucharest",
    "Bukarest", "Valladolid", "Halle", "London", "Zlín", "Stockholm", "Zwickau",
    "Zwikau", "Massy", "Turin", "Paris", "Dortmund", "St. Gallen", "Copenhagen",
    "Debrecen", "Zurich", "New York", "Barcelona", "Madrid", "Rome", "Amsterdam",
]

VENUE_WORDS = re.compile(
    r"\b(?:Hall|Center|Centre|Church|Cathedral|Auditorium|Theatre|Theater|"
    r"Musiktheater|Stadthalle|Zeneakadémia|Factory163)\b",
)
KNOWN_VENUES = [
    "Kongresszusi Központ", "Palacio Euskalduna", "Piedmont Piano Company",
    "Liszt Institute", "Kölcsey Centre", "De Montfort Hall", "Greaves Concert Hall",
    "Venice Performing Arts Center", "Community Church of Vero Beach", "The Lyric Theatre",
    "Pasquerilla Performing Arts Center", "Los Altos United Methodist Church",
    "Franz Liszt Music Academy", "Kodály Center", "Factory163",
    "Stadthalle", "Philharmonie de Paris", "Auditorium Rai Arturo Toscanini",
]


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip(" .,-")


def _country(text):
    lowered = text.lower()
    for name in sorted(COUNTRIES, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered):
            return COUNTRIES[name]
    return None


def _city(text):
    lowered = text.casefold()
    for city in sorted(CITY_NAMES, key=len, reverse=True):
        if city.casefold() in lowered:
            return "Bucharest" if city == "Bukarest" else "Zwickau" if city == "Zwikau" else city
    return None


def _venue(text, city):
    lowered = text.casefold()
    if re.search(r'Auditorium Rai\s+["“]?Arturo Toscanini', text, re.I):
        return 'Auditorium Rai "Arturo Toscanini"'
    for known in KNOWN_VENUES:
        if known.casefold() in lowered:
            return known
    for match in VENUE_WORDS.finditer(text):
        before = text[:match.end()]
        clause = re.split(r"[.!?;]|\s+-\s+", before)[-1]
        words = clause.strip(" ,").split()
        venue = _clean(" ".join(words[-6:]))
        venue = re.sub(r"^(?:USA|UK|Spain|Germany|Hungary|France)[.,]?\s+", "", venue, flags=re.I)
        if (venue and venue.casefold() != city.casefold() and len(venue) <= 90
                and not re.search(r"\b(?:Orchestra|Orch|Symphony|Symphonie)\b", venue, re.I)):
            return venue
    return None


def _dates(match):
    month = MONTHS[match.group("month").lower().rstrip(".")]
    year = int(match.group("year"))
    days = [int(day) for day in re.findall(r"\d{1,2}", match.group("days"))]
    result = []
    for day in days:
        try:
            result.append(datetime(year, month, day).date().isoformat())
        except ValueError:
            continue
    return result


def _time(text):
    match = re.search(r"\b(\d{1,2})[.:;](\d{2})\s*(a\.?m\.?|p\.?m\.?)?", text, re.I)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    suffix = (match.group(3) or "").lower()
    if suffix.startswith("p") and hour < 12:
        hour += 12
    elif suffix.startswith("a") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _title(text, city):
    remainder = DATE_RE.sub("", text, count=1).strip(" .,-")
    parts = [_clean(part) for part in re.split(r"[.!?]", remainder) if _clean(part)]
    for part in parts:
        if (city.casefold() not in part.casefold() and not _country(part) and len(part) >= 8
                and not re.fullmatch(r"\d{1,2}[.:;]\d{2}\s*[ap]\.?m\.?,?", part, re.I)
                and not re.search(r"\b(?:concert alert|full concert details?)\b", part, re.I)):
            return part[:240]
    return f"Miklós Rózsa performance — {city}"


def _detail_url(container, page_url):
    if container is None:
        return page_url
    for link in container.find_all("a", href=True):
        href = urljoin(page_url, link["href"])
        if not any(host in href for host in ("sheetmusicplus.com", "youtube.com", "facebook.com")):
            return href
    return page_url


def _current_entries(soup):
    """Yield the page's hand-authored event paragraphs without nested duplicates."""
    seen = set()
    for paragraph in soup.find_all("p"):
        fragments = []
        for child in paragraph.contents:
            if getattr(child, "name", None) == "p":
                continue
            if hasattr(child, "get_text"):
                fragments.append(child.get_text(" ", strip=True))
            else:
                fragments.append(str(child))
        text = _clean(" ".join(fragments))
        match = DATE_RE.match(text)
        if not match:
            continue
        # Broken legacy markup often nests the following paragraphs. Stop at
        # the next independently dated occurrence.
        following = DATE_RE.search(text, match.end())
        if following:
            text = text[:following.start()].strip()
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        sibling = paragraph.find_next_sibling()
        if sibling and sibling.name in {"ul", "ol"}:
            text = _clean(f"{text} {sibling.get_text(' ', strip=True)}")
        yield text, paragraph


def _archive_entries(soup):
    # Archive records are separated by BR elements rather than semantic tags.
    for line in soup.get_text("\n", strip=True).splitlines():
        text = _clean(line)
        if DATE_RE.match(text):
            yield text, None


def _records(entries, page_url):
    records = []
    for text, container in entries:
        match = DATE_RE.match(text)
        if not match or "unconfirmed" in text.lower():
            continue
        country_code = _country(text)
        city = _city(text)
        if not country_code or not city:
            continue
        venue = _venue(text, city)
        if not venue:
            continue
        description = _clean(text)
        for event_date in _dates(match):
            records.append({
                "title": _title(text, city),
                "date": event_date,
                "url": _detail_url(container, page_url),
                "time_from": _time(match.group(0) + " " + text[match.end():]),
                "time_to": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description,
            })
    return records


class MiklosRozsaSocietyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="mrs_miklosrozsa_info",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["date", "time_from", "venue", "city", "title"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        for page_url, archive in ((PERFORMANCES_URL, False), (ARCHIVE_URL, True)):
            log_message("Fetching concert listing", event="crawler_url_fetch", url=page_url)
            try:
                response = requests.get(page_url, headers=HEADERS, timeout=30)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    "Concert listing fetch failed",
                    event="crawler_url_fetch_failed",
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            soup = BeautifulSoup(response.content, "html.parser")
            entries = _archive_entries(soup) if archive else _current_entries(soup)
            records.extend(_records(entries, page_url))
        log_message("Concert records parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    MiklosRozsaSocietyCrawler().run()


if __name__ == "__main__":
    main()
