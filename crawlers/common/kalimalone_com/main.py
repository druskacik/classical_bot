import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Kali Malone"
SOURCE_URL = "https://kalimalone.com/events/"

MONTHS = {
    name: number
    for number, name in enumerate(
        [
            "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
            "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
        ],
        1,
    )
}

COUNTRIES = {
    "australia": "AU", "austria": "AT", "belgium": "BE", "canada": "CA",
    "croatia": "HR", "czech republic": "CZ", "denmark": "DK", "england": "GB",
    "estonia": "EE", "finland": "FI", "france": "FR", "germany": "DE",
    "greece": "GR", "iceland": "IS", "italy": "IT", "japan": "JP",
    "ireland": "IE", "lithuania": "LT", "luxembourg": "LU", "luxemburg": "LU",
    "mexico": "MX", "monaco": "MC", "netherlands": "NL",
    "new zealand": "NZ", "norway": "NO", "poland": "PL", "portugal": "PT",
    "scotland": "GB", "slovenia": "SI", "spain": "ES", "sweden": "SE",
    "switzerland": "CH", "the netherlands": "NL", "usa": "US",
    "united states": "US", "wales": "GB",
    "au": "AT", "be": "BE", "bl": "BE", "ca": "CA", "ch": "CH", "de": "DE",
    "dk": "DK", "ee": "EE", "es": "ES", "fi": "FI", "fr": "FR", "gb": "GB",
    "gr": "GR", "hr": "HR", "it": "IT", "lt": "LT", "mx": "MX", "nl": "NL",
    "nyc": "US", "pl": "PL", "pt": "PT", "se": "SE", "uk": "GB", "us": "US",
}

US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana",
    "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
    "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "az", "ca", "co", "dc", "il", "ma", "md", "mn", "mo", "ne", "ny", "pa",
    "tn", "tx", "vt", "wa",
}

CITY_COUNTRIES = {
    "aalborg": "DK", "aarhus": "DK", "barcelona": "ES", "berlin": "DE",
    "brussels": "BE", "copenhagen": "DK", "ghent": "BE", "helsinki": "FI",
    "lisbon": "PT", "london": "GB", "madrid": "ES", "milan": "IT",
    "montreal": "CA", "new york city": "US", "nyc": "US", "paris": "FR",
    "stockholm": "SE", "torino": "IT", "toronto": "CA", "vienna": "AT",
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip(" \n\t|,")


def _place_parts(raw_place: str) -> tuple[str, str] | None:
    place = _clean(
        re.split(
            r",\s*(?:with|w/|live|solo|a/v|acousmatic|upper|sorrowing|xchan|“)",
            raw_place,
            maxsplit=1,
            flags=re.I,
        )[0]
    )
    if not place:
        return None

    country_code = None
    city = place
    lower_place = place.lower().rstrip(".")
    for qualifier in sorted(COUNTRIES, key=len, reverse=True):
        if re.search(rf"(?:^|[,\s]){re.escape(qualifier)}$", lower_place):
            city = _clean(re.sub(rf"[,\s]+{re.escape(qualifier)}$", "", place, flags=re.I))
            country_code = COUNTRIES[qualifier]
            break

    for state in sorted(US_STATES, key=len, reverse=True):
        if not re.search(rf"(?:^|[,\s]){re.escape(state)}$", lower_place):
            continue
        state_city = _clean(re.sub(rf"[,\s]+{re.escape(state)}$", "", place, flags=re.I))
        if state == "ca" and state_city.lower() in {"montreal", "toronto"}:
            break
        city = state_city
        country_code = "US"
        break
    if lower_place.endswith(" bc") or lower_place.endswith(",bc"):
        city = _clean(re.sub(r"[,\s]+bc$", "", place, flags=re.I))
        country_code = "CA"
    if country_code == "US":
        for state in sorted(US_STATES, key=len, reverse=True):
            if re.search(rf"(?:^|[,\s]){re.escape(state)}$", city, flags=re.I):
                city = _clean(re.sub(rf"[,\s]+{re.escape(state)}$", "", city, flags=re.I))
                break
    if not country_code:
        country_code = CITY_COUNTRIES.get(city.lower())
    if not country_code:
        return None
    return city, country_code


def _dates(year: int, month_name: str, day_expression: str) -> list[str]:
    # A plus denotes separately advertised performances. Hyphenated spans on this
    # page are festival/installation ranges without concrete daily occurrences.
    if "-" in day_expression or "–" in day_expression:
        return []
    days = [int(value) for value in re.findall(r"\d{1,2}", day_expression)]
    parsed = []
    for day in days:
        try:
            parsed.append(date(year, MONTHS[month_name], day).isoformat())
        except ValueError:
            continue
    return parsed


def parse_events(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    year = None
    records = []

    for paragraph in soup.select("main p, #content p"):
        text = _clean(paragraph.get_text(" ", strip=True))
        if re.fullmatch(r"20\d{2}", text):
            year = int(text)
            continue
        if year is None or "@" not in text:
            continue

        match = re.match(
            r"^(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+([\d\s+\-–]+)\s*\|",
            text,
            flags=re.I,
        )
        if not match:
            continue
        month_name = match.group(1).upper()
        event_dates = _dates(year, month_name, match.group(2))
        if not event_dates:
            continue

        place_element = paragraph.find("em")
        raw_place = (
            place_element.get_text(" ", strip=True)
            if place_element
            else text.split("|", 1)[1].split("@", 1)[0]
        )
        place_parts = _place_parts(raw_place)
        if not place_parts:
            continue
        city, country_code = place_parts

        after_at = text.split("@", 1)[1]
        venue = _clean(re.split(r"\s*[\[{]", after_at, maxsplit=1)[0])
        venue = _clean(
            re.split(
                r"\s+(?:[A-Z]{3,9}\s+\d{1,2}|\d{1,2}\.\d{1,2})\b",
                venue,
                maxsplit=1,
                flags=re.I,
            )[0]
        )
        if not venue:
            continue

        link = paragraph.find("a", href=True)
        url = urljoin(SOURCE_URL, link["href"]) if link else SOURCE_URL
        detail = None
        detail_match = re.search(r"[\[{]([^\]}]+)[\]}]?\s*$", text)
        if detail_match:
            detail = _clean(detail_match.group(1))
        title = f"Kali Malone — {detail}" if detail else f"Kali Malone at {venue}"

        for event_date in event_dates:
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": text,
                }
            )
    return records


class KaliMaloneCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="kalimalone_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "url", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching live archive", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        records = parse_events(response.text)
        log_message(
            "Parsed live archive",
            event="crawler_parse_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    KaliMaloneCrawler().run()


if __name__ == "__main__":
    main()
