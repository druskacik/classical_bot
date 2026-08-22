import re
from datetime import datetime
from urllib.parse import urldefrag

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Nicholas Rimmer"
SOURCE_URL = "https://nicholasrimmer.com/"
CONCERTS_URL = "https://nicholasrimmer.com/en/concerts/"

# The calendar is the itinerary of a German pianist, but it contains tours in
# several countries. Unmarked German cities are the site's normal case; the
# exceptions below cover the foreign cities used by the published archive.
NON_GERMAN_CITIES = {
    "amsterdam": "NL",
    "bantry": "IE",
    "batavia": "US",
    "beimwil": "CH",
    "beinwil": "CH",
    "belfast": "GB",
    "bern": "CH",
    "boswil": "CH",
    "cambridge": "GB",
    "cardiff": "GB",
    "catania": "IT",
    "chichester": "GB",
    "cowbridge": "GB",
    "eisenstadt": "AT",
    "ernen": "CH",
    "florenz": "IT",
    "fort myers": "US",
    "gateshead": "GB",
    "guernsey": "GB",
    "halifax": "GB",
    "helsingborg": "SE",
    "helsinborg": "SE",
    "helsinki": "FI",
    "innsbruck": "AT",
    "kemperton": "US",
    "key west": "US",
    "kimberton": "US",
    "lammermuir": "GB",
    "liestal": "CH",
    "london": "GB",
    "lockenhaus": "AT",
    "lewes": "GB",
    "luzern": "CH",
    "manchester": "GB",
    "mantova": "IT",
    "milverton": "GB",
    "mönsterås": "SE",
    "nantesbuch": "DE",
    "palermo": "IT",
    "portsmouth": "GB",
    "rheinfelden": "CH",
    "rovereto": "IT",
    "sevenoaks": "GB",
    "sevenaoks": "GB",
    "sheffield": "GB",
    "st. gerold": "AT",
    "stamford": "GB",
    "uppsala": "SE",
    "växjö": "SE",
    "weesp": "NL",
    "west wicklow": "IE",
}

COUNTRY_MARKERS = {
    "AT": "AT",
    "CH": "CH",
    "FI": "FI",
    "IE": "IE",
    "IRL": "IE",
    "IT": "IT",
    "NL": "NL",
    "SE": "SE",
    "UK": "GB",
}

VENUE_WORDS = re.compile(
    r"\b(?:auditorium|center|centre|church|concertgebouw|foyer|hall|haus|"
    r"kirche|konzerthaus|kulturforum|kulturzentrum|martinstadl|museum|"
    r"philharmonie|sala|saal|schloss|stadthalle|theater|wigmore)\b",
    re.IGNORECASE,
)


def _country_and_city(label: str) -> tuple[str, str]:
    label = re.sub(r"\s+", " ", label).strip()
    label = re.sub(
        r"\s+-\s+(?:CANCELLED|Leider\s+(?:corona(?:bedingt)?\s+)?abgesagt).*$",
        "",
        label,
        flags=re.IGNORECASE,
    ).strip()
    if label.casefold().startswith("festspiele mecklenburg vorpommern"):
        return "DE", "Beidendorf"
    marker = re.search(r"\((AT|CH|FI|IE|IRL|IT|NL|SE|UK)\)", label, re.IGNORECASE)
    if marker:
        country_code = COUNTRY_MARKERS[marker.group(1).upper()]
        city = (label[: marker.start()] + label[marker.end() :]).strip(" ,-/")
        return country_code, city

    lower = label.casefold()
    if "netherlands" in lower:
        return "NL", re.sub(r",?\s*Netherlands", "", label, flags=re.IGNORECASE).strip()
    if "sweden" in lower:
        return "SE", re.sub(r",?\s*Sweden", "", label, flags=re.IGNORECASE).strip()
    if re.search(r",\s*(?:FL|IL|PA)\b", label):
        return "US", re.sub(r",\s*(?:FL|IL|PA)\b", "", label).strip()

    lookup = re.split(r"\s*[-/]\s*", label, maxsplit=1)[0].casefold()
    return NON_GERMAN_CITIES.get(lookup, "DE"), label


def _venue(lines: list[str]) -> str | None:
    for line in reversed(lines):
        if line.startswith("@"):
            venue = line[1:].split("|")[0].strip()
            if venue and not venue.startswith(("http://", "https://")) and len(venue) <= 120:
                return venue
    for line in reversed(lines):
        candidate = line.split("|")[0].strip()
        if (
            len(candidate) <= 120
            and not candidate.startswith(("http://", "https://"))
            and VENUE_WORDS.search(candidate)
        ):
            return candidate
    return None


def _parse_event(box) -> dict | None:
    headings = box.find_all("h4", recursive=False)
    if len(headings) < 2:
        return None

    date_text = headings[0].get_text(" ", strip=True)
    # Multi-day festival ranges are overview records rather than concrete
    # occurrences, so deliberately do not turn them into a single event.
    if re.search(r"\d{2}/\d{2}/\d{4}\s+-\s+\d{2}/\d{2}/\d{4}", date_text):
        return None
    match = re.fullmatch(
        r"(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}))?(?:\s+-\s+(\d{2}:\d{2}))?\s*",
        date_text,
    )
    if not match:
        return None
    try:
        event_date = datetime.strptime(match.group(1), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None

    link = headings[1].find("a", href=True)
    if not link:
        return None
    location_label = link.get_text(" ", strip=True)
    country_code, city = _country_and_city(location_label)

    detail_lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in box.get_text("\n").splitlines()
        if line.strip()
    ][2:]
    venue = _venue(detail_lines)
    if not city or not venue or venue.casefold() == city.casefold():
        return None

    description = "\n".join(detail_lines) or None
    return {
        "title": f"Nicholas Rimmer in {city}",
        "date": event_date,
        "url": urldefrag(link["href"])[0],
        "time_from": match.group(2),
        "time_to": match.group(3),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class NicholasRimmerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nicholasrimmer_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert archive", event="crawler_url_fetch", url=CONCERTS_URL)
        try:
            response = requests.get(CONCERTS_URL, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                "Concert archive request failed",
                event="crawler_url_fetch_failed",
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.content, "html.parser")
        records = [record for box in soup.select("div.whitebox") if (record := _parse_event(box))]
        log_message(
            "Concert archive parsed",
            event="crawler_scrape_completed",
            url=CONCERTS_URL,
            record_count=len(records),
        )
        return records


def main():
    NicholasRimmerCrawler().run()


if __name__ == "__main__":
    main()
