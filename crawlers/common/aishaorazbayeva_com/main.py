import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://aishaorazbayeva.com/"
SOURCE = "Aisha Orazbayeva"
API_URL = urljoin(
    SOURCE_URL,
    "_api/v0/site/aishaorazbayeva/projects?type=page&offset=0&limit=40",
)
ARCHIVE_PATHS = ["2020-2024", *map(str, range(2012, 2020))]
HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": urljoin(SOURCE_URL, "Calendar-1"),
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "X-Requested-With": "XMLHttpRequest",
}

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        1,
    )
}
MONTHS["feburary"] = 2  # Misspelling present in the first-party calendar.

# The calendar is an international touring diary and often omits the country.
# These mappings turn only explicit city/place evidence into an ISO country code.
PLACES = {
    "aarhus": ("Aarhus", "DK"), "almaty": ("Almaty", "KZ"),
    "antwerp": ("Antwerp", "BE"), "berchem": ("Berchem", "BE"),
    "bern": ("Bern", "CH"), "beveren": ("Beveren", "BE"),
    "belfast": ("Belfast", "GB"), "bornem": ("Bornem", "BE"),
    "brugge": ("Bruges", "BE"), "brussels": ("Brussels", "BE"),
    "cambridge": ("Cambridge", "GB"), "charleroi": ("Charleroi", "BE"),
    "deinze": ("Deinze", "BE"), "dresden": ("Dresden", "DE"),
    "dublin": ("Dublin", "IE"), "geneva": ("Geneva", "CH"),
    "ghent": ("Ghent", "BE"), "kortrijk": ("Kortrijk", "BE"),
    "koksijde": ("Koksijde", "BE"), "la-salvetat-sur-agoût": ("La Salvetat-sur-Agout", "FR"),
    "lamalou les bains": ("Lamalou-les-Bains", "FR"), "limoges": ("Limoges", "FR"),
    "london": ("London", "GB"), "mechlen": ("Mechelen", "BE"),
    "milan": ("Milan", "IT"), "montpellier": ("Montpellier", "FR"),
    "mons la trivalle": ("Mons-la-Trivalle", "FR"), "oslo": ("Oslo", "NO"),
    "paris": ("Paris", "FR"), "pelt": ("Pelt", "BE"),
    "rome": ("Rome", "IT"), "roujan": ("Roujan", "FR"),
    "saint-martin-de-l'arçon": ("Saint-Martin-de-l'Arçon", "FR"),
    "st pons": ("Saint-Pons-de-Thomières", "FR"), "strasbourg": ("Strasbourg", "FR"),
    "sint niklaas": ("Sint-Niklaas", "BE"), "tunis": ("Tunis", "TN"),
    "turin": ("Turin", "IT"), "vélieux": ("Vélieux", "FR"),
    "vienna": ("Vienna", "AT"), "warsaw": ("Warsaw", "PL"),
}

VENUE_WORDS = re.compile(
    r"\b(?:abbaye|abbatiale|academy|archipel|arts? venue|botanique|cafe|café|"
    r"castle|casino|centre|chapelle|château|church|concertgebouw|corso|factory|"
    r"festspielhaus|gallery|hall|kaaitheatre|king's place|maison|museum|opera|opéra|"
    r"pallazo|palazzo|projects|salle|theatre|theater|église|waterfront)\b",
    re.I,
)
NON_VENUE = re.compile(
    r"^(?:with|by|works? by|music for|solo set|details?|more info|tickets?|to reserve|"
    r"festival d'|spitalfields festival|ultima festival|warsaw autumn)",
    re.I,
)
DATE_RE = re.compile(
    r"^(?P<days>\d{1,2}(?:\s*(?:-|–|/|&|and)\s*\d{1,2})*)\s+"
    r"(?P<month>[A-Za-z]+)$",
    re.I,
)


def _clean_lines(html):
    html = re.sub(r"(?:<br\s*/?>\s*){2,}", "\n\n", html, flags=re.I)
    html = re.sub(r"<br\s*/?>\s*", "\n", html, flags=re.I)
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup.select("script, style"):
        unwanted.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text().splitlines()]
    cleaned = []
    for line in lines:
        if line or (cleaned and cleaned[-1]):
            cleaned.append(line)
    return cleaned


def _blocks(lines):
    year = None
    block = []
    for line in lines + [""]:
        if re.fullmatch(r"20\d{2}", line):
            if block:
                yield year, block
                block = []
            year = int(line)
        elif not line:
            if block:
                yield year, block
                block = []
        else:
            block.append(line)


def _dates(text, year):
    match = DATE_RE.match(text)
    if not match or not year:
        return []
    month = MONTHS.get(match.group("month").lower())
    if not month:
        return []
    day_text = match.group("days")
    numbers = [int(value) for value in re.findall(r"\d+", day_text)]
    if re.search(r"[-–]", day_text) and len(numbers) == 2:
        numbers = list(range(numbers[0], numbers[1] + 1))
    dates = []
    for day in numbers:
        try:
            dates.append(datetime(year, month, day).date().isoformat())
        except ValueError:
            continue
    return dates


def _place(lines):
    for index in range(len(lines) - 1, -1, -1):
        folded = lines[index].casefold()
        for needle, result in PLACES.items():
            if needle in folded:
                return index, result
    return None, None


def _time(lines):
    text = " ".join(lines)
    match = re.search(r"\b(\d{1,2})(?:[.:h](\d{2}))?\s*(am|pm)?\b", text, re.I)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    suffix = (match.group(3) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _venue(lines, city_index):
    city_line = lines[city_index]
    if VENUE_WORDS.search(city_line):
        return re.sub(r",?\s*\b\d{1,2}(?:[.:h]\d{2})\s*(?:am|pm)?\b.*$", "", city_line, flags=re.I).strip(" ,")
    candidates = lines[1:city_index + 1]
    for line in reversed(candidates):
        if VENUE_WORDS.search(line) and not NON_VENUE.search(line):
            return re.sub(r",?\s*\b\d{1,2}(?:[.:h]\d{2})\s*(?:am|pm)?\b.*$", "", line, flags=re.I).strip(" ,")
    if city_index > 1:
        candidate = lines[city_index - 1]
        invalid = re.search(r"\b(?:duo|ensemble|festival|ictus|recital)\b", candidate, re.I)
        if (
            candidate.casefold() != lines[1].casefold()
            and candidate.casefold() != "heimkommen"
            and not invalid
            and not NON_VENUE.search(candidate)
            and not any(key in candidate.casefold() for key in PLACES)
        ):
            return candidate
    return None


def _parse_calendar(html, page_url):
    records = []
    for year, lines in _blocks(_clean_lines(html)):
        dates = _dates(lines[0], year) if lines else []
        if not dates or len(lines) < 3:
            continue
        city_index, place = _place(lines[1:])
        if place is None:
            continue
        city_index += 1
        venue = _venue(lines, city_index)
        if not venue:
            continue
        title = lines[1]
        if title.casefold() == venue.casefold():
            title = f"Aisha Orazbayeva at {venue}"
        description = "\n".join(lines[1:])
        for event_date in dates:
            records.append({
                "title": title, "date": event_date, "url": page_url,
                "time_from": _time(lines[2:]), "venue": venue,
                "city": place[0], "country_code": place[1],
                "description": description, "source_url": SOURCE_URL, "source": SOURCE,
            })
    return records


class AishaOrazbayevaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="aishaorazbayeva_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        log_message("Fetching calendar API", event="crawler_url_fetch", url=API_URL)
        response = session.get(API_URL, timeout=30)
        response.raise_for_status()
        calendar = next(
            page for page in response.json() if page.get("project_url") == "Calendar-1"
        )
        records = _parse_calendar(calendar.get("content") or "", urljoin(SOURCE_URL, "Calendar-1"))

        for path in ARCHIVE_PATHS:
            url = urljoin(SOURCE_URL, path)
            try:
                log_message("Fetching calendar archive", event="crawler_url_fetch", url=url)
                archive_response = session.get(url, timeout=30)
                archive_response.raise_for_status()
                soup = BeautifulSoup(archive_response.text, "html.parser")
                content = soup.select_one(".project_content")
                if content:
                    records.extend(_parse_calendar(str(content), url))
            except requests.RequestException as error:
                log_message(
                    "Calendar archive fetch failed", event="crawler_url_fetch_failed", url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        log_message("Calendar parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    AishaOrazbayevaCrawler().run()


if __name__ == "__main__":
    main()
