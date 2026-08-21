import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "http://handonahkur.com/handos-music/performances/"
SOURCE = "Hando Nahkur"
ARCHIVE_YEARS = range(2012, 2018)
COUNTRIES = {
    "USA": "US",
    "United States": "US",
    "Estonia": "EE",
    "Finland": "FI",
    "Germany": "DE",
    "Italy": "IT",
    "Sweden": "SE",
    "Canada": "CA",
    "United Kingdom": "GB",
    "UK": "GB",
    "Russia": "RU",
    "Switzerland": "CH",
    "Holland": "NL",
}
MULTIWORD_NORTH_AMERICAN_CITIES = (
    "Fort Worth", "New York", "San Francisco", "Los Angeles",
    "Salt Lake City", "Santa Fe", "Kansas City", "Oklahoma City",
)
INTERNATIONAL_CITIES = (
    "St.Moritz", "Zurich", "Savonlinna", "Tallinn", "Vääna-Jõesuu",
    "Munich", "Ulm", "Treviso", "Bergen", "Elva",
)


def _get_soup(url: str) -> BeautifulSoup:
    log_message("Fetching performance listing", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _parse_date(value: str) -> str | None:
    value = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value.strip())
    for pattern in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def _country(location: str) -> str | None:
    last_part = location.rsplit(",", 1)[-1].strip().rstrip(".")
    return next(
        (code for name, code in COUNTRIES.items() if re.match(rf"{re.escape(name)}\b", last_part)),
        None,
    )


def _venue_from_title(title: str) -> str | None:
    title = re.sub(r"\s+-\s+www\.[^ ]+\s*$", "", title).strip()
    at_match = re.search(r"\bat\s+(.+)$", title, flags=re.IGNORECASE)
    if at_match:
        return at_match.group(1).strip()
    if re.search(r"\b(?:livestream|streaming)\b", title, flags=re.IGNORECASE):
        return None
    return title or None


def _current_records(soup: BeautifulSoup) -> list[dict]:
    records = []
    for item in soup.select(".concert-item"):
        date_node = item.select_one(".date")
        title_node = item.select_one(".concert-title")
        location_nodes = item.select("p.item-title:not(.date)")
        if not date_node or not title_node or not location_nodes:
            continue
        date = _parse_date(date_node.get_text(" ", strip=True))
        title = title_node.get_text(" ", strip=True)
        location = location_nodes[-1].get_text(" ", strip=True)
        city = location.split(",", 1)[0].strip()
        country_code = _country(location)
        venue = _venue_from_title(title)
        if not all((date, title, venue, city, country_code)):
            continue
        records.append({
            "title": title,
            "date": date,
            "url": SOURCE_URL,
            "time_from": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": None,
        })
    return records


def _archive_records(soup: BeautifulSoup, url: str) -> list[dict]:
    records = []
    line_pattern = re.compile(
        r"^([A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?,\s+\d{4})\s*[–—-]\s*(.+)$"
    )
    for node in soup.select("p"):
        text = node.get_text(" ", strip=True)
        match = line_pattern.match(text)
        if not match:
            continue
        date = _parse_date(match.group(1))
        body = match.group(2).strip()
        country_code = _country(body)
        parts = [part.strip() for part in body.split(",")]
        if not date or not country_code or len(parts) < 2:
            continue

        # Archive rows have no separate fields. US/Canadian rows consistently
        # end in "City, State, Country", even when the event text has commas.
        city = None
        if country_code in {"US", "CA"} and len(parts) >= 3:
            city_and_prefix = parts[-3].strip()
            city = next(
                (name for name in MULTIWORD_NORTH_AMERICAN_CITIES if city_and_prefix.endswith(name)),
                city_and_prefix.rsplit(" ", 1)[-1],
            )
        else:
            location_prefix = body.rsplit(",", 1)[0].strip()
            city = next(
                (name for name in INTERNATIONAL_CITIES if location_prefix.endswith(name)),
                None,
            )
        if not city:
            continue
        city_match = list(re.finditer(rf"\b{re.escape(city)}\b", body, re.IGNORECASE))
        if not city_match:
            continue
        split = city_match[-1]
        title = body[: split.start()].strip(" ,-–—")
        venue_text = re.sub(r"\s*\([^)]*\)\s*", " ", title).strip(" ,-–—")
        venue = _venue_from_title(venue_text)
        if not title or not venue:
            continue
        records.append({
            "title": title,
            "date": date,
            "url": url,
            "time_from": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": None,
        })
    return records


class HandonahkurCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="handonahkur_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city", "country_code"],
    )

    def scrape(self) -> list[dict]:
        records = _current_records(_get_soup(SOURCE_URL))
        for year in ARCHIVE_YEARS:
            url = f"http://handonahkur.com/{year}-performances/"
            try:
                records.extend(_archive_records(_get_soup(url), url))
            except requests.RequestException as error:
                log_message(
                    "Performance archive fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return records


def main():
    HandonahkurCrawler().run()


if __name__ == "__main__":
    main()
