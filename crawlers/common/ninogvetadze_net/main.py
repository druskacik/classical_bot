import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.ninogvetadze.net/"
TOUR_URL = f"{SOURCE_URL}tour"
SOURCE = "Nino Gvetadze"

COUNTRY_CODES = {"AT", "DE", "IT", "NL"}

# The tour page often names a festival or town instead of the room. These
# first-party labels and their linked presenter pages provide stable evidence
# for the following venue/city pairs.
LOCATION_RULES = (
    ("theaterdeveste.nl", "Theater de Veste", "Delft"),
    ("classic-con-brio.de", "St. Matthäus Kirche", "Melle"),
    ("duivenvoordeconcerten.nl", "Dorpskerk Voorschoten", "Voorschoten"),
    ("schlossfestspiele.de", "Ordenssaal", "Ludwigsburg"),
    ("schloss-elmau.de", "Schloss Elmau", "Elmau"),
    ("kissingersommer.de", "Rossini-Saal", "Bad Kissingen"),
    ("rheingau-musik-festival.de", "Schloss Johannisberg", "Geisenheim"),
    ("concertgebouw.nl", "Concertgebouw", "Amsterdam"),
)


def _clean_lines(card):
    lines = []
    for line in card.get_text("\n", strip=True).splitlines():
        line = re.sub(r"\s+", " ", line).strip(" ,")
        if line and line.lower() != "more info" and line not in lines:
            lines.append(line)
    return lines


def _event_url(card):
    links = [link.get("href") for link in card.find_all("a", href=True)]
    links = [link for link in links if link.startswith("http")]
    return links[-1] if links else TOUR_URL


def _location(url, lines):
    for marker, venue, city in LOCATION_RULES:
        if marker in url:
            return venue, city

    # Future entries can still be accepted when the artist explicitly supplies
    # a conventional "venue, city, CC" line.
    joined = " ".join(lines)
    match = re.search(
        r"(?P<venue>[^,]+),\s*(?P<city>[^,]+),\s*(?:AT|DE|IT|NL)\b", joined
    )
    if match:
        return match.group("venue").strip(), match.group("city").strip()
    return None, None


def _parse_date(text, year):
    match = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)", text.strip())
    if not match:
        # A range denotes a festival residency, not a concrete performance.
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2)} {year}", "%d %B %Y"
        ).date().isoformat()
    except ValueError:
        return None


def _title(lines):
    content = [line for line in lines[1:] if line not in COUNTRY_CODES]
    if not content:
        return None

    programme_at = next(
        (
            index
            for index, line in enumerate(content)
            if re.search(
                r"\b(?:Bach|Beethoven|Bernstein|Brahms|Chausson|Chopin|Dohn|Dvorak|Faur|Mendelssohn|Mozart|Rachmaninov|Saint|Schumann|Shostakovich|Sibelius|Tchaikovsky)\b",
                line,
                re.I,
            )
        ),
        len(content),
    )
    title_lines = content[:programme_at]
    return " — ".join(title_lines[:3]) if title_lines else content[0]


class NinoGvetadzeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="ninogvetadze_net",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "url", "venue"],
    )

    def scrape(self):
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=TOUR_URL)
        response = requests.get(TOUR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        heading = soup.find(
            ["h1", "h2"], string=re.compile(r"\b20\d{2}\b")
        )
        if not heading:
            log_message(
                "Concert calendar has no season year",
                event="crawler_parse_warning",
                url=TOUR_URL,
            )
            return []
        year = int(re.search(r"\b(20\d{2})\b", heading.get_text()).group(1))

        cards = []
        for card in soup.select("div.gpDCD5"):
            # Wix nests a second gpDCD5 inside each visual card.
            if card.find_parent("div", class_="gpDCD5") is None:
                cards.append(card)

        records = []
        for card in cards:
            lines = _clean_lines(card)
            if not lines:
                continue
            event_date = _parse_date(lines[0], year)
            if not event_date:
                continue

            country_code = next((line for line in lines if line in COUNTRY_CODES), None)
            if country_code is None:
                match = re.search(r",\s*(AT|DE|IT|NL)\b", " ".join(lines))
                country_code = match.group(1) if match else None

            url = _event_url(card)
            venue, city = _location(url, lines)
            title = _title(lines)
            if not all((title, country_code, venue, city)):
                log_message(
                    "Skipping concert with incomplete location data",
                    event="crawler_record_skipped",
                    url=url,
                )
                continue

            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": None,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": "\n".join(lines[1:]),
                }
            )

        log_message(
            "Concert calendar parsed",
            event="crawler_scrape_completed",
            url=TOUR_URL,
            record_count=len(records),
        )
        return records


def main():
    NinoGvetadzeCrawler().run()


if __name__ == "__main__":
    main()
