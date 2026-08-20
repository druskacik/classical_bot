import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Anastasia Rizikov"
SOURCE_URL = "https://www.anastasiarizikov.com/"
EVENT_PAGES = (
    "https://www.anastasiarizikov.com/events-eng",
    "https://www.anastasiarizikov.com/past-events",
)

COUNTRY_CODES = {
    "BELGIUM": "BE",
    "CANADA": "CA",
    "CHINA": "CN",
    "FRANCE": "FR",
    "POLAND": "PL",
}

DATE_RE = re.compile(
    r"^(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
    r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2})(?:\s+@\s+(\d{1,2}:\d{2}))?$",
    re.I,
)
YEAR_RE = re.compile(r"^20\d{2}$")
GRID_RE = re.compile(
    r"\.fe-block-([\w-]+)\s*\{[^{}]*?grid-area:\s*(\d+)\s*/\s*(\d+)",
    re.S,
)


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _grid_positions(section):
    positions = {}
    for style in section.find_all("style"):
        for block_id, row, column in GRID_RE.findall(style.get_text()):
            # The later rule is the desktop layout inside the media query.
            positions[block_id] = (int(row), int(column))
    return positions


def _venue_from(block, title):
    for link in block.find_all("a", href=True):
        if "map" in link["href"].lower():
            venue = _clean(link.get_text(" "))
            if venue:
                return venue
    match = re.search(r"\b(?:AT|IN)\s+(.+)$", title, re.I)
    venue = _clean(match.group(1)) if match else None
    # A festival/series name is not a physical venue.
    if venue and re.search(r"\b(?:FESTIVAL|MUSIQUES EN)\b", venue, re.I):
        return None
    return venue


def _parse_section(section, page_url):
    positions = _grid_positions(section)
    rows = {}
    for block in section.select(".fe-block"):
        classes = block.get("class", [])
        block_id = next((c.removeprefix("fe-block-") for c in classes if c.startswith("fe-block-")), None)
        if not block_id or block_id not in positions:
            continue
        text = _clean(block.get_text(" "))
        if text:
            row, column = positions[block_id]
            rows.setdefault(row, []).append((column, text, block))

    ordered = [(row, sorted(values)) for row, values in sorted(rows.items())]
    explicit_years = [int(text) for _, values in ordered for _, text, _ in values if YEAR_RE.fullmatch(text)]
    year = explicit_years[0] - 1 if explicit_years else None
    records = []

    for _, values in ordered:
        year_value = next((int(text) for _, text, _ in values if YEAR_RE.fullmatch(text)), None)
        if year_value:
            year = year_value
            continue

        date_item = next(((text, block) for _, text, block in values if DATE_RE.fullmatch(text)), None)
        if not date_item or year is None:
            continue
        date_match = DATE_RE.fullmatch(date_item[0])
        location = next((text for _, text, _ in values if "," in text and text.rsplit(",", 1)[1].strip().upper() in COUNTRY_CODES), None)
        title_item = next(
            ((text, block) for column, text, block in values if column > 8 and text not in {"INFO", "FREE ENTRY", "RSVP ONLY"}),
            None,
        )
        if not location or not title_item:
            continue

        city, country = (_clean(part) for part in location.rsplit(",", 1))
        country_code = COUNTRY_CODES.get(country.upper())
        title, title_block = title_item
        if re.search(r"\bPRIVATE\b", title, re.I):
            continue
        venue = _venue_from(title_block, title)
        if not country_code or not city or not venue:
            continue

        info_link = next(
            (link["href"] for _, _, block in values for link in block.find_all("a", href=True)
             if "map" not in link["href"].lower() and link["href"] != "#"),
            page_url,
        )
        try:
            event_date = datetime.strptime(
                f"{year} {date_match.group(1)} {date_match.group(2)}", "%Y %B %d"
            ).date().isoformat()
        except ValueError:
            continue

        records.append({
            "title": title,
            "date": event_date,
            "url": info_link,
            "time_from": date_match.group(3),
            "venue": venue,
            "city": city.title(),
            "country_code": country_code,
            "description": title,
        })
    return records


def scrape_page(url):
    log_message("Fetching event listing", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    records = []
    for section in soup.select("main section[data-test='page-section']"):
        records.extend(_parse_section(section, url))
    return records


class AnastasiaRizikovCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="anastasiarizikov_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="CA",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "city", "venue"],
    )

    def scrape(self):
        records = []
        for url in EVENT_PAGES:
            records.extend(scrape_page(url))
        return records


def main():
    AnastasiaRizikovCrawler().run()


if __name__ == "__main__":
    main()
