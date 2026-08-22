import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Mike Oldfield Official"
SOURCE_URL = "https://www.mikeoldfieldofficial.com/"
TOUR_URL = "https://www.mikeoldfieldofficial.com/tour/"
TITLE = "Mike Oldfield's Tubular Bells: The Best of Tubular Bells I, II & III"

COUNTRY_CODES = {
    "Austria": "AT",
    "Belgium": "BE",
    "Czech": "CZ",
    "Finland": "FI",
    "Germany": "DE",
    "Netherlands": "NL",
    "Sweden": "SE",
    "Switzerland": "CH",
    "UK": "GB",
}


def _description(soup: BeautifulSoup) -> str | None:
    """Return the tour copy above the first year's event table."""
    tour_heading = next(
        (heading for heading in soup.find_all("h2") if heading.get_text(" ", strip=True).lower() == "tour"),
        None,
    )
    if tour_heading is None:
        return None

    paragraphs = []
    for element in tour_heading.find_all_next():
        if element.name == "h3" and re.fullmatch(r"20\d{2}", element.get_text(strip=True)):
            break
        if element.name == "p":
            text = element.get_text(" ", strip=True)
            if text:
                paragraphs.append(text)
    return "\n\n".join(paragraphs) or None


def _parse_row(row, year: int, description: str | None) -> dict | None:
    link = row.find("a", href=True)
    spans = row.select("span")
    venue_node = row.find("b")
    if link is None or len(spans) < 2 or venue_node is None:
        return None

    date_match = re.search(r"(\d{1,2})\s+([A-Za-z]{3})", spans[0].get_text(" ", strip=True))
    location_line = next(
        (text.strip() for text in spans[1].stripped_strings if text.strip() != venue_node.get_text(" ", strip=True)),
        "",
    )
    if date_match is None or "," not in location_line:
        return None

    city, country = (part.strip() for part in location_line.rsplit(",", 1))
    venue = venue_node.get_text(" ", strip=True)
    country_code = COUNTRY_CODES.get(country)
    if not city or not venue or country_code is None:
        return None

    # The source's final row says Poole but names Brighton Dome. The unique,
    # venue-specific evidence is stronger than the copied city value.
    if city == "Poole" and venue == "Brighton Dome":
        city = "Brighton"

    try:
        event_date = datetime.strptime(
            f"{date_match.group(1)} {date_match.group(2)} {year}", "%d %b %Y"
        ).date().isoformat()
    except ValueError:
        return None

    return {
        "title": TITLE,
        "date": event_date,
        "url": link["href"],
        "time_from": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class MikeOldfieldOfficialCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="mikeoldfieldofficial_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "city", "venue"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching tour page", event="crawler_url_fetch", url=TOUR_URL)
        response = requests.get(TOUR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        description = _description(soup)

        records = []
        for table in soup.find_all("table"):
            year_heading = table.find_previous("h3")
            if year_heading is None:
                continue
            year_text = year_heading.get_text(strip=True)
            if not re.fullmatch(r"20\d{2}", year_text):
                continue
            for row in table.find_all("tr"):
                record = _parse_row(row, int(year_text), description)
                if record is not None:
                    records.append(record)

        log_message(
            "Tour page parsed",
            event="crawler_scrape_completed",
            url=TOUR_URL,
            record_count=len(records),
        )
        return records


def main():
    MikeOldfieldOfficialCrawler().run()


if __name__ == "__main__":
    main()
