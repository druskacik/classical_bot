import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.eratoalakiozidou.com/"
NEWS_URL = urljoin(SOURCE_URL, "news.html")
SOURCE = "Erato Alakiozidou"

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7,
    "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# The archive describes tours, so locations must be resolved per occurrence.
LOCATIONS = {
    "alexandroupolis": ("Alexandroupolis", "GR"),
    "amsterdam": ("Amsterdam", "NL"),
    "athens": ("Athens", "GR"),
    "aveiro": ("Aveiro", "PT"),
    "caiazzo": ("Caiazzo", "IT"),
    "caserta": ("Caserta", "IT"),
    "florina": ("Florina", "GR"),
    "izmir": ("Izmir", "TR"),
    "i̇zmir": ("Izmir", "TR"),
    "kalamaria": ("Kalamaria", "GR"),
    "kalamata": ("Kalamata", "GR"),
    "krakow": ("Krakow", "PL"),
    "lamporecchio": ("Lamporecchio", "IT"),
    "larisa": ("Larisa", "GR"),
    "larissa": ("Larisa", "GR"),
    "mantova": ("Mantua", "IT"),
    "monopoli": ("Monopoli", "IT"),
    "panorama": ("Panorama", "GR"),
    "pistoia": ("Pistoia", "IT"),
    "rome": ("Rome", "IT"),
    "thessaloniki": ("Thessaloniki", "GR"),
    "timisoara": ("Timisoara", "RO"),
    "volos": ("Volos", "GR"),
}

VENUES = {
    "aassm": "Ahmed Adnan Saygun Arts Center",
    "ahmed adnan saygun sanat merkezi": "Ahmed Adnan Saygun Arts Center",
    "athens conservatoire": "Athens Conservatoire",
    "athens conservatory": "Athens Conservatoire",
    "megaron concert hall": "Athens Concert Hall",
    "macedonia university concert hall": "University of Macedonia Concert Hall",
    "municipal conservatory of alexandroupolis": "Municipal Conservatory of Alexandroupolis",
    "municipal conservatory of kalamaria": "Municipal Conservatory of Kalamaria",
    "kalamaria municipal concert hall": "Kalamaria Municipal Concert Hall",
    "larisa municipal conservatory": "Municipal Conservatory of Larisa",
    "state conservatory of thessaloniki": "State Conservatory of Thessaloniki",
    "thessaloniki concert hall": "Thessaloniki Concert Hall",
    "florina conservatory": "Florina Conservatory",
    "sagrestia del borromini": "Sagrestia del Borromini",
    "villa rospigliosi": "Villa Rospigliosi",
    "villa magni": "Villa Magni",
    "palazzo ganucci cancellieri": "Palazzo Ganucci Cancellieri",
    "chieza di s.francesco": "Chiesa di San Francesco",
    "chiesa di s.francesco": "Chiesa di San Francesco",
}

VENUE_LOCATIONS = {
    "Athens Concert Hall": ("Athens", "GR"),
    "Athens Conservatoire": ("Athens", "GR"),
    "Chiesa di San Francesco": ("Caiazzo", "IT"),
    "Florina Conservatory": ("Florina", "GR"),
    "Kalamaria Municipal Concert Hall": ("Kalamaria", "GR"),
    "Municipal Conservatory of Alexandroupolis": ("Alexandroupolis", "GR"),
    "Municipal Conservatory of Kalamaria": ("Kalamaria", "GR"),
    "Municipal Conservatory of Larisa": ("Larisa", "GR"),
    "State Conservatory of Thessaloniki": ("Thessaloniki", "GR"),
    "Thessaloniki Concert Hall": ("Thessaloniki", "GR"),
    "University of Macedonia Concert Hall": ("Thessaloniki", "GR"),
}

EVENT_WORDS = re.compile(
    r"\b(concert|recital|performance|premiere|festival|tango|world premieres?)\b",
    re.IGNORECASE,
)
NON_EVENT_WORDS = re.compile(
    r"\b(interview|review|recording|release|available|tickets?|press|media|season|"
    r"workshop|masterclass|master class|student concert|competition|conference|symposium|call for|"
    r"postcast|podcast|radio)\b",
    re.IGNORECASE,
)


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


def _parse_date(text: str) -> str | None:
    word_patterns = (
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]+)[,\s]+(20\d{2}|19\d{2})\b",
        r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2}|19\d{2})\b",
    )
    for index, pattern in enumerate(word_patterns):
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        if index == 0:
            day, month_name, year = match.groups()
        else:
            month_name, day, year = match.groups()
        month = MONTHS.get(month_name.casefold().rstrip("."))
        if month:
            try:
                return date(int(year), month, int(day)).isoformat()
            except ValueError:
                return None

    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2}|19\d{2}|\d{2})\b", text)
    if match:
        day, month, year = map(int, match.groups())
        year += 2000 if year < 100 else 0
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    years = set(re.findall(r"\b(?:19|20)\d{2}\b", text))
    if len(years) == 1:
        match = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]+)\b",
            text,
            re.IGNORECASE,
        )
        if match:
            day, month_name = match.groups()
            month = MONTHS.get(month_name.casefold().rstrip("."))
            if month:
                try:
                    return date(int(next(iter(years))), month, int(day)).isoformat()
                except ValueError:
                    return None
    return None


def _parse_time(text: str) -> str | None:
    match = re.search(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?:\s*(?:pm|am))?", text, re.IGNORECASE)
    if not match:
        return None
    hour, minute = map(int, match.groups())
    suffix = match.group(0).lower()
    if "pm" in suffix and hour < 12:
        hour += 12
    if "am" in suffix and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _location(text: str) -> tuple[str, str] | None:
    folded = _fold(text)
    matches = [(folded.find(_fold(key)), value) for key, value in LOCATIONS.items()]
    matches = [item for item in matches if item[0] >= 0]
    return min(matches, default=(0, None), key=lambda item: item[0])[1]


def _venue(text: str) -> str | None:
    folded = _fold(text)
    matches = [(folded.find(_fold(key)), value) for key, value in VENUES.items()]
    matches = [item for item in matches if item[0] >= 0]
    return min(matches, default=(0, None), key=lambda item: item[0])[1]


def _sections(soup: BeautifulSoup):
    """Yield each archive heading with content up to the next heading."""
    headings = soup.select("#wsite-content h2.wsite-content-title")
    for heading in headings:
        title = heading.get_text(" ", strip=True)
        if not title or title.casefold() == "news":
            continue
        parts: list[str] = []
        for node in heading.next_elements:
            if node is heading:
                continue
            if isinstance(node, Tag) and node.name == "h2" and "wsite-content-title" in node.get("class", []):
                break
            if isinstance(node, Tag) and node.name in {"script", "style"}:
                continue
            if isinstance(node, str) and node.strip():
                parts.append(node.strip())
        description = re.sub(r"\s+", " ", " ".join(parts)).strip()
        yield title, description


class EratoAlakiozidouCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="eratoalakiozidou_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert archive", event="crawler_url_fetch", url=NEWS_URL)
        response = requests.get(NEWS_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for title, description in _sections(soup):
            combined = f"{title} {description}"
            # News is a mixed archive with no categories. Keep only concrete,
            # independently dated event posts; overview and editorial posts go
            # neither straight to classical nor into malformed candidate rows.
            if not EVENT_WORDS.search(combined) or NON_EVENT_WORDS.search(title):
                continue
            event_date = _parse_date(combined)
            venue = _venue(combined)
            location = VENUE_LOCATIONS.get(venue) if venue else None
            location = location or _location(combined)
            if not event_date or not location or not venue:
                continue
            city, country_code = location
            records.append({
                "title": title,
                "date": event_date,
                "url": NEWS_URL,
                "time_from": _parse_time(combined),
                "time_to": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description or None,
            })

        log_message(
            "Concert archive parsed",
            event="crawler_scrape_completed",
            url=NEWS_URL,
            record_count=len(records),
        )
        return records


def main():
    EratoAlakiozidouCrawler().run()


if __name__ == "__main__":
    main()
