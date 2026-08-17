import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "http://www.albanyconsort.com/"
SOURCE = "The Albany Consort"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; classical-concert-crawler/1.0)"}
DATE_RE = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))$",
    re.IGNORECASE,
)


def _lines(soup: BeautifulSoup) -> list[str]:
    paragraphs = soup.find_all("p")
    elements = paragraphs if paragraphs else [soup]
    return [
        re.sub(r"\s+", " ", line).strip()
        for element in elements
        for line in element.get_text(" ", strip=True).splitlines()
        if re.sub(r"\s+", " ", line).strip()
    ]


def _time(value: str) -> str:
    return datetime.strptime(value.replace(" ", "").upper(), "%I:%M%p").strftime("%H:%M") \
        if ":" in value else datetime.strptime(value.replace(" ", "").upper(), "%I%p").strftime("%H:%M")


def _page_year(text: str, url: str) -> int | None:
    festival_year = re.search(r"Festival(?:\s+Fringe)?\s+(20\d{2})", text, re.IGNORECASE)
    if festival_year:
        return int(festival_year.group(1))
    url_year = re.search(r"(?:^|\D)(2\d)(?:\D|$)", url.rstrip("/").rsplit("/", 1)[-1])
    return 2000 + int(url_year.group(1)) if url_year else None


def _default_location(lines: list[str], first_date: int) -> tuple[str | None, str | None]:
    header = lines[:first_date]
    for index, line in enumerate(header):
        if line.lower().startswith("at the ") and index + 1 < len(header):
            venue = line[3:].rstrip(",")
            city_match = re.search(r",\s*([A-Za-z .'-]+)$", header[index + 1])
            return venue, city_match.group(1).strip() if city_match else None
    return None, None


def _block_location(block: list[str], venue: str, city: str) -> tuple[str, str]:
    for line in block:
        match = re.search(r"VENUE:\s*([^,]+),.*?,\s*([A-Z][A-Z ]+)$", line)
        if match:
            return match.group(1).strip(), match.group(2).strip().title()
    return venue, city


def parse_concert_page(soup: BeautifulSoup, url: str) -> list[dict]:
    lines = _lines(soup)
    date_indexes = [index for index, line in enumerate(lines) if DATE_RE.match(line)]
    if not date_indexes:
        return []

    year = _page_year("\n".join(lines), url)
    default_venue, default_city = _default_location(lines, date_indexes[0])
    if year is None or not default_venue or not default_city:
        return []

    records = []
    for position, start in enumerate(date_indexes):
        end = date_indexes[position + 1] if position + 1 < len(date_indexes) else len(lines)
        match = DATE_RE.match(lines[start])
        block = lines[start + 1:end]
        if not block:
            continue
        venue, city = _block_location(block, default_venue, default_city)
        try:
            event_date = datetime.strptime(
                f"{match.group(1)} {match.group(2)} {year}", "%B %d %Y"
            ).date().isoformat()
        except ValueError:
            continue

        description_lines = block[:]
        if start > 0 and "CANCELLED" in lines[start - 1].upper():
            description_lines.insert(0, lines[start - 1])
        records.append(
            {
                "title": block[0],
                "date": event_date,
                "url": url,
                "time_from": _time(match.group(3)),
                "venue": venue,
                "city": city,
                "description": "\n".join(description_lines),
            }
        )
    return records


class AlbanyConsortCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="albanyconsort_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching Albany Consort calendar", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
        response.raise_for_status()
        home = BeautifulSoup(response.content, "html.parser")
        calendar_link = next(
            (
                link.get("href")
                for link in home.find_all("a", href=True)
                if "next concert" in link.get_text(" ", strip=True).lower()
            ),
            None,
        )
        if not calendar_link:
            log_message("No concert calendar link found", event="crawler_parse_completed", record_count=0)
            return []

        calendar_url = urljoin(response.url, calendar_link)
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=calendar_url)
        calendar_response = requests.get(calendar_url, headers=HEADERS, timeout=30)
        calendar_response.raise_for_status()
        records = parse_concert_page(BeautifulSoup(calendar_response.content, "html.parser"), calendar_response.url)
        log_message("Concert records parsed", event="crawler_parse_completed", record_count=len(records))
        return records


def main():
    AlbanyConsortCrawler().run()


if __name__ == "__main__":
    main()
