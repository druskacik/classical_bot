import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Arte dei Suonatori"
SOURCE_URL = "https://www.artedeisuonatori.com/"
CONCERTS_URL = urljoin(SOURCE_URL, "concerts")

COUNTRY_CODES = {
    "austria": "AT",
    "belgium": "BE",
    "denmark": "DK",
    "france": "FR",
    "germany": "DE",
    "island": "IS",  # The site uses "Island" for Iceland.
    "poland": "PL",
    "polska": "PL",
    "romania": "RO",
    "sweden": "SE",
}


def clean_text(element) -> str:
    if element is None:
        return ""
    text = element.get_text("\n", strip=True).replace("\u200d", "")
    lines = [re.sub(r"\s+", " ", line).strip(" /\t") for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_location(value: str) -> tuple[str, str]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) >= 2 and parts[0].casefold() in COUNTRY_CODES:
        return parts[1], COUNTRY_CODES[parts[0].casefold()]

    # Country-less locations in this Polish orchestra's calendar are known
    # Polish tour stops (for example Gliwice, Lewków, and Żelazowa Wola).
    return parts[0] if parts else "", "PL"


def parse_time(value: str) -> str | None:
    match = re.search(r"\b([01]\d|2[0-3]):[0-5]\d\b", value)
    return match.group(0) if match else None


def parse_concert(item) -> dict | None:
    sections = item.select(".calendar__item-section")
    if len(sections) < 3:
        return None

    title = clean_text(item.select_one('[fs-cmsfilter-field="title"]'))
    date_text = clean_text(item.select_one('[fs-cmsfilter-field="date"]'))
    location_text = clean_text(item.select_one('[fs-cmsfilter-field="location"]'))
    venue = clean_text(sections[2].select_one(".calendar__item-details"))
    link = item.select_one('a[href*="/dates/"]')
    if not all((title, date_text, location_text, venue, link and link.get("href"))):
        return None

    try:
        concert_date = datetime.strptime(date_text, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None

    city, country_code = parse_location(location_text)
    if not city:
        return None

    headings = sections[0].find_all("h1")
    time_text = clean_text(headings[1]) if len(headings) > 1 else ""
    program = clean_text(item.select_one('[fs-cmsfilter-field="program"]'))
    artists = clean_text(item.select_one('[fs-cmsfilter-field="artists"]'))
    description_parts = []
    if program:
        description_parts.append(program)
    if artists:
        description_parts.append(f"Artists\n{artists}")

    return {
        "title": title,
        "date": concert_date,
        "url": urljoin(SOURCE_URL, link["href"]),
        "time_from": parse_time(time_text),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": "\n\n".join(description_parts) or None,
    }


class ArteDeiSuonatoriCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="artedeisuonatori_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="PL",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=CONCERTS_URL)
        response = requests.get(
            CONCERTS_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        skipped_count = 0
        for item in soup.select(".calendar__item"):
            record = parse_concert(item)
            if record is None:
                skipped_count += 1
                continue
            records.append(record)

        log_message(
            "Concert calendar parsed",
            event="crawler_parse_completed",
            url=CONCERTS_URL,
            record_count=len(records),
            skipped_count=skipped_count,
        )
        return records


def main():
    ArteDeiSuonatoriCrawler().run()


if __name__ == "__main__":
    main()
