import html
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.calliopetsoupaki.com/"
SOURCE = "Calliope Tsoupaki"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/posts"
NEWS_CATEGORY_ID = 1

# The news archive has no event taxonomy and mixes concerts with interviews,
# prizes, recordings, and other news.  These first-party venue names let us
# retain only posts from which both a real venue and its city can be established.
VENUES = (
    (r"Augustijnenkerk", "Augustijnenkerk", "Dordrecht", "NL"),
    (r"Muziekgebouw aan ['’]t IJ|Muziekgebouw", "Muziekgebouw aan 't IJ", "Amsterdam", "NL"),
    (r"Tivoli\s*[|/]?\s*Vredenburg|TivoliVredenburg", "TivoliVredenburg", "Utrecht", "NL"),
    (r"La Passerelle", "La Passerelle, Scène Nationale", "Saint-Brieuc", "FR"),
    (r"Th[ée]âtre Silvia Monfort", "Théâtre Silvia Monfort", "Paris", "FR"),
    (r"Concertgebouw", "Concertgebouw", "Amsterdam", "NL"),
    (r"Splendor", "Splendor", "Amsterdam", "NL"),
    (r"De Rode Deur", "De Rode Deur", "Almere", "NL"),
    (r"Meervaart", "Meervaart", "Amsterdam", "NL"),
    (r"Goudse Schouwburg", "De Goudse Schouwburg", "Gouda", "NL"),
    (r"SPOT\s*\(Stadsschouwburg\)|Stadsschouwburg Groningen", "Stadsschouwburg Groningen", "Groningen", "NL"),
    (r"De Kring", "De Kring", "Roosendaal", "NL"),
    (r"Purmaryn", "Theater de Purmaryn", "Purmerend", "NL"),
    (r"Schouwburg Concertzaal Tilburg", "Schouwburg Concertzaal Tilburg", "Tilburg", "NL"),
    (r"Theater De Leest", "Theater De Leest", "Waalwijk", "NL"),
    (r"Het Nationale Theater", "Het Nationale Theater", "The Hague", "NL"),
    (r"Kraakhuis", "Kraakhuis", "Ghent", "BE"),
    (r"Church of St\. Francis Xavier", "Church of St. Francis Xavier", "New York", "US"),
    (r"Grote Sint LaurensKerk|Grote Kerk of Alkmaar", "Grote Sint Laurenskerk", "Alkmaar", "NL"),
    (r"Amare", "Amare", "The Hague", "NL"),
    (r"Groene Kathedraal", "De Groene Kathedraal", "Almere", "NL"),
    (r"Verkadefabriek", "Verkadefabriek", "'s-Hertogenbosch", "NL"),
    (r"de Doelen", "de Doelen", "Rotterdam", "NL"),
    (r"Dom Cathedral|\bDom, Utrecht", "Dom Church", "Utrecht", "NL"),
)

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
        1,
    )
}
MONTH_PATTERN = "|".join(MONTHS)
DATE_PATTERNS = (
    re.compile(rf"\b(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,)?\s+(?P<year>20\d{{2}})\b", re.I),
    re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+(?P<month>{MONTH_PATTERN})(?:\s*,)?\s+(?P<year>20\d{{2}})\b", re.I),
    re.compile(r"\b(?P<day>\d{1,2})[/-](?P<month_num>\d{1,2})[/-](?P<year>20\d{2}|\d{2})\b"),
)
YEARLESS_DATE = re.compile(
    rf"\b(?:(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})|(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+(?P<month2>{MONTH_PATTERN}))\b",
    re.I,
)
TIME_PATTERN = re.compile(r"\b(?P<hour>[01]?\d|2[0-3])[:.](?P<minute>[0-5]\d)\b")
EVENT_EVIDENCE = re.compile(r"\b(concert|premiere|performed|performance|recital|tour)\b", re.I)
NON_EVENT_TITLE = re.compile(r"\b(cd|review|interview|playlist|lecture|prize|award)\b", re.I)


def _text_from_html(rendered: str) -> str:
    soup = BeautifulSoup(rendered, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return re.sub(r"[ \t]+", " ", soup.get_text("\n", strip=True))


def _parse_date(match: re.Match, fallback_year: int | None = None) -> date | None:
    groups = match.groupdict()
    year_text = groups.get("year")
    year = int(year_text) if year_text else fallback_year
    if year is None:
        return None
    if year < 100:
        year += 2000
    month = int(groups["month_num"]) if groups.get("month_num") else MONTHS[(groups.get("month") or groups.get("month2")).lower()]
    day = int(groups.get("day") or groups.get("day2"))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dates(text: str, post_year: int) -> list[tuple[date, int]]:
    found: list[tuple[date, int]] = []
    occupied: list[tuple[int, int]] = []
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            parsed = _parse_date(match)
            if parsed:
                found.append((parsed, match.start()))
                occupied.append(match.span())

    # The author routinely omits the year in an announcement made shortly
    # before a performance. Infer only the post's own year, never a future year.
    for match in YEARLESS_DATE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        parsed = _parse_date(match, post_year)
        if parsed and parsed not in {value for value, _ in found}:
            found.append((parsed, match.start()))
    return sorted(set(found))


def _nearest_venue(text: str, position: int):
    matches = []
    for pattern, venue, city, country_code in VENUES:
        for match in re.finditer(pattern, text, re.I):
            matches.append((abs(match.start() - position), venue, city, country_code, match.start()))
    if not matches:
        return None
    following = [item for item in matches if item[4] >= position and item[0] <= 80]
    distance, venue, city, country_code, venue_position = min(following or matches)
    # Avoid attaching a remote background reference to a date. The beginning
    # of a compact announcement is allowed a little more room.
    if distance > 80:
        return None
    return venue, city, country_code


def _time_near(text: str, position: int) -> str | None:
    window = text[position:position + 180]
    start_match = re.search(r"start(?:s|ing)?(?:\s+at)?\s+(?P<hour>[01]?\d|2[0-3])[:.](?P<minute>[0-5]\d)", window, re.I)
    match = start_match or TIME_PATTERN.search(window)
    if not match:
        return None
    return f"{int(match.group('hour')):02d}:{match.group('minute')}"


def _records_from_post(post: dict) -> list[dict]:
    title = html.unescape(BeautifulSoup(post["title"]["rendered"], "html.parser").get_text(" ", strip=True))
    text = _text_from_html(post["content"]["rendered"])
    lead = text[:1800]
    if NON_EVENT_TITLE.search(title) or not EVENT_EVIDENCE.search(lead):
        return []

    records = []
    post_year = datetime.fromisoformat(post["date"]).year
    for concert_date, position in _dates(lead, post_year):
        location = _nearest_venue(lead, position)
        if not location:
            continue
        time_from = _time_near(lead, position)
        # Later prose often mentions contextual dates (anniversaries, reviews,
        # broadcasts). Undated-time occurrences are accepted only in the
        # announcement's opening section.
        if position > 400 and time_from is None:
            continue
        venue, city, country_code = location
        records.append(
            {
                "title": title,
                "date": concert_date.isoformat(),
                "url": post["link"],
                "time_from": time_from,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": text,
            }
        )
    return records


class CalliopeTsoupakiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="calliopetsoupaki_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NL",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "url", "venue"],
    )

    def scrape(self) -> list[dict]:
        records = []
        page = 1
        while True:
            log_message("Fetching news API page", event="crawler_url_fetch", url=API_URL, page=page)
            response = requests.get(
                API_URL,
                params={"categories": NEWS_CATEGORY_ID, "per_page": 100, "page": page, "_fields": "date,link,title,content"},
                timeout=30,
            )
            response.raise_for_status()
            posts = response.json()
            for post in posts:
                records.extend(_records_from_post(post))
            total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
            if page >= total_pages:
                break
            page += 1
        log_message("Candidate extraction completed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    CalliopeTsoupakiCrawler().run()


if __name__ == "__main__":
    main()
