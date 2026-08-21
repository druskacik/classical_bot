import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Vladimir Horowitz Piano Competition"
SOURCE_URL = "https://legacy.horowitzv.org/"
TOURS_URL = "https://legacy.horowitzv.org/eng-home-2021/tours.html"

COUNTRY_CODES = {
    "Bosnia and Herzegovina": "BA",
    "Croatia": "HR",
    "Egypt": "EG",
    "Estonia": "EE",
    "France": "FR",
    "Germany": "DE",
    "Italy": "IT",
    "Japan": "JP",
    "Luxembourg": "LU",
    "Morocco": "MA",
    "Norway": "NO",
    "Poland": "PL",
    "Spain": "ES",
    "Ukraine": "UA",
    "USA": "US",
}

DATE_FORMATS = ("%d %B %Y", "%d.%m.%Y", "%d.%m.%y", "%d.%b.%Y")
NON_VENUE_PREFIXES = (
    "with ", "present", "presentation", "artistic ", "conductor", "chief ",
    "senior ", "soloist", "the opening", "international competition",
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip(" \t,.")


def _parse_date(value: str) -> str | None:
    value = _clean(value)
    # A range or list may represent several venues and cannot safely be expanded.
    if re.search(r"\d\s*[-,]\s*\d", value) or value.lower().startswith("season"):
        return None
    roman = {"I": "01", "II": "02", "III": "03", "IV": "04", "V": "05",
             "VI": "06", "VII": "07", "VIII": "08", "IX": "09", "X": "10",
             "XI": "11", "XII": "12"}
    match = re.fullmatch(r"(\d{1,2})\.([IVX]+)\.(\d{4})", value, re.I)
    if match and match.group(2).upper() in roman:
        value = f"{match.group(1)}.{roman[match.group(2).upper()]}.{match.group(3)}"
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _split_city_venue(value: str) -> tuple[str, str | None]:
    value = _clean(value)
    if "." in value:
        city, venue = value.split(".", 1)
        return _clean(city), _clean(venue) or None
    if "," in value:
        city, venue = value.split(",", 1)
        return _clean(city), _clean(venue) or None
    return value, None


def _looks_like_venue(value: str) -> bool:
    lower = value.lower()
    if not value or lower.startswith(NON_VENUE_PREFIXES):
        return False
    if re.search(r"\b(prize|prizewinner|laureate|conductor)\b", lower):
        return False
    return bool(re.search(
        r"\b(hall|opera|theatre|theater|philharmonic|filharmonia|studio|"
        r"university|academy|conservatoire|palace|embassy|synagogue|lyceum|hotel)\b",
        lower,
    ))


def _normalize_venue(value: str) -> str:
    value = _clean(value.strip('"'))
    hall = re.search(r"(Lysenko Column Hall)", value, re.I)
    if hall:
        return hall.group(1)
    embassy = re.search(r"(Embassy of Ukraine)", value, re.I)
    if embassy:
        return embassy.group(1)
    return value


def _location(lines: list[str]) -> tuple[str, str, str] | None:
    if len(lines) < 3:
        return None
    country_line = _clean(lines[1])
    country = next((name for name in COUNTRY_CODES if country_line == name or country_line.startswith(name + ",")), None)
    if country is None:
        return None

    remainder = _clean(country_line[len(country):])
    offset = 2
    if remainder:
        city, inline_venue = _split_city_venue(remainder)
    else:
        if len(lines) < 4:
            return None
        city, inline_venue = _split_city_venue(lines[2])
        offset = 3

    # Multiple named cities describe a tour, not one safely locatable occurrence.
    if not city or "," in city or " and " in city.lower():
        return None
    venue = inline_venue
    if venue is None and len(lines) > offset and _looks_like_venue(_clean(lines[offset])):
        venue = _clean(lines[offset])
    if not venue or not _looks_like_venue(venue):
        return None
    return city, _normalize_venue(venue), COUNTRY_CODES[country]


def parse_tours(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("#mainColIn")
    if content is None:
        raise ValueError("Tours page does not contain #mainColIn")

    records = []
    for paragraph in content.find_all("p"):
        lines = [_clean(line) for line in paragraph.get_text("\n").splitlines() if _clean(line)]
        if not lines:
            continue
        event_date = _parse_date(lines[0])
        location = _location(lines) if event_date else None
        if not event_date or not location:
            continue
        city, venue, country_code = location
        records.append({
            "title": "Horowitz Competition Laureates Concert",
            "date": event_date,
            "url": TOURS_URL,
            "time_from": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": "\n".join(lines),
        })
    return records


class LegacyHorowitzCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="legacy_horowitzv_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "venue", "city", "country_code"],
        front_fields=[("source_url", TOURS_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert archive", event="crawler_url_fetch", url=TOURS_URL)
        try:
            response = requests.get(TOURS_URL, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                "Concert archive fetch failed",
                event="crawler_url_fetch_failed",
                url=TOURS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        records = parse_tours(response.text)
        log_message("Concert archive parsed", event="crawler_parse_completed", record_count=len(records))
        return records


def main():
    LegacyHorowitzCrawler().run()


if __name__ == "__main__":
    main()
