import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.azamali.com/"
TOUR_URL = "https://www.azamali.com/tour"
SOURCE = "Azam Ali"

# The archive uses free-form prose and changes the order of city and venue from
# year to year.  These first-party venue names make the published locations
# unambiguous; unknown future formats are deliberately skipped.
LOCATION_RULES = (
    ("University of Florida Performing Arts", "University of Florida Performing Arts", "Gainesville", "US"),
    ("Rudolstadt Festival", "Rudolstadt Festival", "Rudolstadt", "DE"),
    ("Detroit Institute of the Arts", "Detroit Institute of the Arts", "Detroit", "US"),
    ("Tempe Center for the Arts", "Tempe Center for the Arts", "Tempe", "US"),
    ("Halbritter Center", "Halbritter Center for the Performing Arts", "Huntingdon", "US"),
    ("Le Poisson Rouge", "Le Poisson Rouge", "New York City", "US"),
    ("Dorothy Menker Theater", "Dorothy Menker Theater", "Palos Hills", "US"),
    ("Metropolitan Museum of Art", "The Metropolitan Museum of Art", "New York City", "US"),
    ("Théâtre Outremont", "Théâtre Outremont", "Montreal", "CA"),
    ("Flato Markham Theatre", "Flato Markham Theatre", "Markham", "CA"),
    ("Centennial Hall", "Centennial Hall - University of Arizona", "Tucson", "US"),
    ("National Hispanic Cultural Center", "National Hispanic Cultural Center", "Albuquerque", "US"),
    ("Meany Hall", "Meany Hall", "Seattle", "US"),
    ("Green Music Center", "Green Music Center", "Rohnert Park", "US"),
    ("Hollywood Palladium", "Hollywood Palladium", "Los Angeles", "US"),
    ("Teregram Ballroom", "Teregram Ballroom", "Los Angeles", "US"),
    ("DROM", "DROM", "New York City", "US"),
    ("Port City Music Hall", "Port City Music Hall", "Portland", "US"),
    ("Carlsen Center", "Carlsen Center", "Overland Park", "US"),
    ("Aventura Arts & Cultural Center", "Aventura Arts & Cultural Center", "Aventura", "US"),
    ("Zorlu Performing Arts Center", "Zorlu Performing Arts Center", "Istanbul", "TR"),
    ("Nevada County Fairgrounds", "Nevada County Fairgrounds", "Grass Valley", "US"),
    ("Freight & Salvage Coffeehouse", "Freight & Salvage Coffeehouse", "Berkeley", "US"),
    ("Taurus Municipal Amphitheater", "Taurus Municipal Amphitheater", "Mersin", "TR"),
    ("Williams Center for the Arts", "Williams Center for the Arts", "Easton", "US"),
    ("Montalvo Arts Center", "Montalvo Arts Center", "Saratoga", "US"),
    ("Northern Arts and Cultural Centre", "Northern Arts and Cultural Centre", "Yellowknife", "CA"),
    ("Festival Place", "Festival Place", "Sherwood Park", "CA"),
    ("Yukon Arts Centre", "Yukon Arts Centre", "Whitehorse", "CA"),
    ("Music Center at Strathmore", "The Music Center at Strathmore", "North Bethesda", "US"),
    ("Michael Schimmel Center", "Michael Schimmel Center for the Arts", "New York City", "US"),
    ("Piper Repertory Theater", "Piper Repertory Theater at Mesa Arts Center", "Mesa", "US"),
    ("Harris Center", "Harris Center", "Folsom", "US"),
    ("Black Box Theatre", "Black Box Theatre at Soka University", "Aliso Viejo", "US"),
    ("Les Dominicains de Haute", "Les Dominicains de Haute-Alsace", "Guebwiller", "FR"),
    ("Café Séraphin", "Café Séraphin", "Guebwiller", "FR"),
)

DATE_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}\b"
)


def _location(text):
    for marker, venue, city, country_code in LOCATION_RULES:
        if marker.casefold() in text.casefold():
            return venue, city, country_code
    return None


def _title(text, venue):
    parts = [part.strip(" -\u00a0") for part in text.split("|")]
    if len(parts) >= 3 and parts[-1]:
        candidate = parts[-1].strip("\u200b")
        normalized_candidate = re.sub(r"\W+", "", candidate).casefold()
        normalized_venue = re.sub(r"\W+", "", venue).casefold()
        if normalized_candidate not in normalized_venue and normalized_venue not in normalized_candidate:
            return candidate

    descriptions = re.findall(r"\(([^()]*(?:Show|Project|Experience)[^()]*)\)", text, re.IGNORECASE)
    if descriptions:
        return f"Azam Ali - {descriptions[-1].strip()}"
    return f"Azam Ali at {venue}"


def parse_tour_page(html):
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for paragraph in soup.find_all("p"):
        text = " ".join(paragraph.stripped_strings)
        date_match = DATE_RE.match(text)
        if not date_match:
            continue

        location = _location(text)
        if location is None:
            log_message(
                "Skipping event with unresolved location",
                event="crawler_event_skipped",
                url=TOUR_URL,
                error_type="UnresolvedLocation",
            )
            continue

        try:
            event_date = datetime.strptime(date_match.group(0), "%B %d, %Y").date().isoformat()
        except ValueError:
            continue

        venue, city, country_code = location
        link = paragraph.find("a", href=True)
        records.append(
            {
                "title": _title(text, venue),
                "date": event_date,
                "url": link["href"] if link else TOUR_URL,
                "time_from": None,
                "time_to": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": text,
            }
        )
    return records


class AzamAliCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="azamali_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching tour archive", event="crawler_url_fetch", url=TOUR_URL)
        response = requests.get(TOUR_URL, timeout=30)
        response.raise_for_status()
        records = parse_tour_page(response.text)
        log_message(
            "Tour archive parsed",
            event="crawler_scrape_completed",
            url=TOUR_URL,
            record_count=len(records),
        )
        return records


def main():
    AzamAliCrawler().run()


if __name__ == "__main__":
    main()
