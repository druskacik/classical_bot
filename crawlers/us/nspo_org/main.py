import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "North Shore Philharmonic Orchestra"
SOURCE_URL = "https://nspo.org/"
CURRENT_URL = urljoin(SOURCE_URL, "concerts.php")
ARCHIVE_URL = urljoin(SOURCE_URL, "past_seasons.php")
TIMEOUT = 30

DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*"
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?"
    r"(?:\s*@\s*(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m))?",
    re.IGNORECASE,
)
SEASON_RE = re.compile(r"\b(19\d{2}|20\d{2})-(?:19|20)?\d{2}\b")

VENUE_CITIES = {
    "Swampscott High School": "Swampscott",
    "Lynn Classical High School": "Lynn",
    "Lynn English High School": "Lynn",
    "St. Anthony's": "Revere",
    "St. Anthony's of Padua": "Revere",
    "St. Richard's": "Danvers",
    "St. Richard Church": "Danvers",
    "Manning Bowl": "Lynn",
    "Memorial Auditorium, Lynn City Hall": "Lynn",
    "Lynn City Hall": "Lynn",
    "North Shore Music Theatre": "Beverly",
}


def clean_text(value: str) -> str:
    return " ".join(value.split())


def fetch(url: str) -> BeautifulSoup:
    log_message("Fetching concert page", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def season_start_year(soup: BeautifulSoup) -> int | None:
    match = SEASON_RE.search(soup.get_text(" ", strip=True))
    return int(match.group(1)) if match else None


def occurrence_year(match: re.Match, season_year: int | None) -> int | None:
    if match.group("year"):
        return int(match.group("year"))
    if season_year is None:
        return None
    month = datetime.strptime(match.group("month")[:3], "%b").month
    return season_year + (month < 7)


def parse_datetime(match: re.Match, season_year: int | None) -> tuple[str, str | None] | None:
    year = occurrence_year(match, season_year)
    if year is None:
        return None
    raw_date = f'{match.group("month")} {match.group("day")} {year}'
    try:
        date_value = datetime.strptime(raw_date, "%B %d %Y").date().isoformat()
    except ValueError:
        return None
    if not match.group("time"):
        return date_value, None
    raw_time = match.group("time").replace(" ", "").upper()
    try:
        time_value = datetime.strptime(raw_time, "%I:%M%p" if ":" in raw_time else "%I%p").time()
    except ValueError:
        return None
    return date_value, time_value.strftime("%H:%M:%S")


def resolve_location(raw: str) -> tuple[str, str] | None:
    location = clean_text(raw).strip(" ,.-")
    location = re.sub(r"\s+(?:Map|Directions)$", "", location, flags=re.IGNORECASE)
    if not location:
        return None
    for venue, city in VENUE_CITIES.items():
        if venue.lower() in location.lower():
            return venue, city
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(parts) >= 2:
        city = re.sub(r"\s+MA$", "", parts[-1], flags=re.IGNORECASE).strip()
        if city and not re.fullmatch(r"MA|Massachusetts", city, re.IGNORECASE):
            return ", ".join(parts[:-1]), city
        if len(parts) >= 3:
            return ", ".join(parts[:-2]), parts[-2]
    return None


def title_for(container: Tag) -> str:
    heading = container.select_one(".section_title_grey, .section_title")
    parts = [clean_text(heading.get_text(" ", strip=True))] if heading else []
    parts.extend(
        clean_text(node.get_text(" ", strip=True))
        for node in container.select(".section_subtitle")
        if clean_text(node.get_text(" ", strip=True))
    )
    return " — ".join(dict.fromkeys(parts)) or "North Shore Philharmonic Orchestra Concert"


def location_after_match(node: Tag, match: re.Match) -> str | None:
    lines = [clean_text(line) for line in node.get_text("\n", strip=True).splitlines() if clean_text(line)]
    for index, line in enumerate(lines):
        if match.group(0).lower() in line.lower() or DATE_RE.search(line):
            if index + 1 < len(lines):
                return lines[index + 1]
    return None


def parse_container(container: Tag, page_url: str, year: int | None) -> list[dict]:
    description = clean_text(container.get_text("\n", strip=True))
    title = title_for(container)
    records = []

    # Modern pages keep each date and venue together in a paragraph. Older
    # archive pages put them in a .conc_info block.
    nodes = container.select(".conc_info") or container.select("p")
    for node in nodes:
        text = clean_text(node.get_text(" ", strip=True))
        for match in DATE_RE.finditer(text):
            parsed = parse_datetime(match, year)
            if not parsed:
                continue
            if "conc_info" in (node.get("class") or []):
                direct = [
                    clean_text(str(item))
                    for item in node.contents
                    if isinstance(item, NavigableString) and clean_text(str(item))
                ]
                location_text = direct[-1] if len(direct) > 1 else None
            else:
                location_text = location_after_match(node, match)
            location = resolve_location(location_text or "")
            if not location:
                continue
            venue, city = location
            anchor = container.get("id")
            url = f"{page_url}#{anchor}" if anchor else page_url
            records.append(
                {
                    "title": title,
                    "date": parsed[0],
                    "url": url,
                    "time_from": parsed[1],
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "description": description,
                }
            )
    return records


def parse_page(soup: BeautifulSoup, page_url: str) -> list[dict]:
    year = season_start_year(soup)
    records = []
    for container in soup.select(".conc_container"):
        records.extend(parse_container(container, page_url, year))
    return records


class NspoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nspo_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        current_soup = fetch(CURRENT_URL)
        records = parse_page(current_soup, CURRENT_URL)

        archive_index = fetch(ARCHIVE_URL)
        archive_urls = sorted(
            {
                urljoin(ARCHIVE_URL, link["href"])
                for link in archive_index.select('a[href*="past_seasons.php?season="]')
            }
        )
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch, url): url for url in archive_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(parse_page(future.result(), url))
                except requests.RequestException as error:
                    log_message(
                        "Skipping unavailable archive page",
                        event="crawler_url_fetch_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        log_message("Concert records parsed", event="crawler_parse_completed", record_count=len(records))
        return records


def main():
    NspoOrgCrawler().run()


if __name__ == "__main__":
    main()
