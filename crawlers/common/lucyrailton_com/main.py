import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Lucy Railton"
SOURCE_URL = "https://lucyrailton.com/"
PERFORMANCES_URL = "https://lucyrailton.com/Performances"
ARCHIVE_URLS = (
    PERFORMANCES_URL,
    "https://lucyrailton.com/Concerts-2020",
    "https://lucyrailton.com/Concerts-2019",
    "https://lucyrailton.com/Concerts-2018",
)

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3,
    "march": 3, "apr": 4, "april": 4, "apil": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8,
    "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "novemeber": 11,
    "dec": 12, "december": 12,
}

# The site is an international touring diary and supplies no structured
# locations. These are cities actually used by its performance archive.
CITY_COUNTRIES = {
    "Aarhus": "DK", "Amsterdam": "NL", "Antwerp": "BE", "Arles": "FR",
    "Athens": "GR", "Barcelona": "ES", "Basel": "CH", "Bergen": "NO",
    "Berlin": "DE", "Bilbao": "ES", "Bologna": "IT", "Braga": "PT",
    "Bratislava": "SK", "Brescia": "IT", "Bristol": "GB", "Brooklyn": "US",
    "Brussels": "BE", "Buenos Aires": "AR", "Cardiff": "GB", "Chicago": "US",
    "Copenhagen": "DK", "Cork": "IE", "Darmstadt": "DE", "Den Haag": "NL",
    "Donegal": "IE", "Dublin": "IE", "Frauenfeld": "CH", "Gdańsk": "PL",
    "Gdansk": "PL", "Glasgow": "GB", "Guadalajara": "MX", "Helsinki": "FI",
    "Huddersfield": "GB", "Kildare": "IE", "Koln": "DE", "Köln": "DE",
    "Kortrijk": "BE", "Kraków": "PL", "Kyoto": "JP", "Leeds": "GB",
    "Le Mans": "FR", "Leuven": "BE", "Lisbon": "PT", "Liverpool": "GB",
    "Ljubljana": "SI", "London": "GB", "Los Angeles": "US", "Luxembourg": "LU",
    "Madrid": "ES", "Manchester": "GB", "Medellin": "CO", "Mexico City": "MX",
    "Milan": "IT", "Modena": "IT", "Montreal": "CA", "Mulhouse": "FR",
    "Munich": "DE", "Murmansk": "RU", "Nantes": "FR", "New York": "US",
    "NYC": "US", "Nottingham": "GB", "Oslo": "NO", "Paris": "FR",
    "Philadelphia": "US", "Ravenna": "IT", "Rome": "IT", "Rotterdam": "NL",
    "Salzburg": "AT", "San Francisco": "US", "Santiago": "CL", "Sao Paulo": "BR",
    "São Paulo": "BR", "Siena": "IT", "Stavanger": "NO", "Stockholm": "SE",
    "Stuttgart": "DE", "Tallin": "EE", "Tallinn": "EE", "The Hague": "NL",
    "Tilburg": "NL", "Tokyo": "JP", "Trondheim": "NO", "Troy": "US",
    "Turku": "FI", "Utrecht": "NL", "Venice": "IT", "Vienna": "AT",
}

NON_EVENTS = re.compile(
    r"\b(residen(?:cy|cies)|album release|radio piece|installation|session and interviews)\b",
    re.I,
)
DATE_PREFIX = re.compile(r"^\s*(\d{1,2})(?:\s*[.\-–]+\s*(\d{1,2}))?[._]?\s+(.*)$")


def _clean(value):
    return re.sub(r"\s+", " ", value).strip(" \n,|")


def _page_lines(soup):
    contents = soup.select(".page_content projectcontent")
    if not contents:
        return []
    content = max(contents, key=lambda node: len(node.get_text()))
    for br in content.select("br"):
        br.replace_with("\n")
    return [_clean(line) for line in content.get_text("", strip=False).splitlines() if _clean(line)]


def _location(text):
    matches = []
    for city, country in CITY_COUNTRIES.items():
        for found in re.finditer(rf"(?<!\w){re.escape(city)}(?!\w)", text, re.I):
            matches.append((found.start(), found.end(), city, country))
    if not matches:
        return None
    start, _, city, country = max(matches, key=lambda item: item[0])
    before = text[:start].rstrip(" ,.|")
    pieces = [_clean(piece) for piece in re.split(r"\s*[|,]\s*", before) if _clean(piece)]
    if len(pieces) < 2:
        return None
    venue = pieces[-1]
    if (
        len(venue.split()) > 8
        or NON_EVENTS.search(venue)
        or ")" in venue
        or re.search(r"\b(?:with|and|performs|featuring)\b", venue, re.I)
    ):
        return None
    return city, country, venue


def _event_days(first, second, year, month):
    last = int(second or first)
    if last < int(first) or last - int(first) > 31:
        return []
    result = []
    for day in range(int(first), last + 1):
        try:
            result.append(date(year, month, day).isoformat())
        except ValueError:
            continue
    return result


def _parse_page(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    lines = _page_lines(soup)
    default_year = int(page_url.rsplit("-", 1)[-1]) if "Concerts-" in page_url else None
    year = default_year
    month = None
    blocks = []
    current = None

    for line in lines:
        year_heading = re.match(r"^Concerts?\D+(20\d{2})", line, re.I)
        if year_heading:
            if current:
                blocks.append(current)
                current = None
            year = int(year_heading.group(1))
            month = None
            continue
        heading = re.fullmatch(
            r"(?:Concerts?\s+)?(" + "|".join(MONTHS) + r")?\s*(20\d{2})?",
            line,
            re.I,
        )
        if heading and (heading.group(1) or heading.group(2)):
            if current:
                blocks.append(current)
                current = None
            if heading.group(1):
                month = MONTHS[heading.group(1).lower()]
            if heading.group(2):
                year = int(heading.group(2))
            continue
        event = DATE_PREFIX.match(line)
        if event and year and month:
            if current:
                blocks.append(current)
            current = [event.group(1), event.group(2), event.group(3), year, month]
        elif current:
            current[2] += " " + line
    if current:
        blocks.append(current)

    records = []
    for first, second, body, event_year, event_month in blocks:
        body = _clean(body)
        if NON_EVENTS.search(body):
            continue
        location = _location(body)
        if not location:
            continue
        city, country, venue = location
        anchor = None
        # Prefer the first event link where its visible text occurs in the block.
        for link in soup.select(".page_content projectcontent a[href]"):
            label = _clean(link.get_text(" ", strip=True))
            if label and label in body and not re.fullmatch(r"20\d{2}", label):
                anchor = urljoin(page_url, link.get("href"))
                break
        title = re.split(r"\s*[|,]\s*", body, maxsplit=1)[0]
        title = _clean(title)
        if not title:
            continue
        for event_date in _event_days(first, second, event_year, event_month):
            records.append({
                "title": title,
                "date": event_date,
                "url": anchor or page_url,
                "time_from": None,
                "time_to": None,
                "venue": venue,
                "city": city,
                "country_code": country,
                "description": body,
            })
    if default_year:
        records.extend(_parse_legacy(lines, default_year, page_url))
    return records


def _parse_legacy(lines, year, page_url):
    records = []
    seen = set()
    start_pattern = re.compile(
        r"^(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:\s*(?:,|and|-)\s*(\d{1,2}))?\s*_\s*(.+)$",
        re.I,
    )
    end_pattern = re.compile(
        r"^_\s*(.+?)[, ]+(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:\s*-\s*(\d{1,2}))?$",
        re.I,
    )
    for line in lines:
        match = start_pattern.match(line)
        if match:
            month, first, second, body = match.groups()
        else:
            match = end_pattern.match(line)
            if not match:
                continue
            body, month, first, second = match.groups()
        body = _clean(body)
        if NON_EVENTS.search(body):
            continue
        location = _location(body)
        if not location:
            continue
        city, country, venue = location
        title = _clean(re.split(r"\s*[|,]\s*", body, maxsplit=1)[0])
        for event_date in _event_days(first, second, year, MONTHS[month.lower()]):
            key = (title.lower(), event_date, venue.lower(), city.lower())
            if key in seen:
                continue
            seen.add(key)
            records.append({
                "title": title,
                "date": event_date,
                "url": page_url,
                "time_from": None,
                "time_to": None,
                "venue": venue,
                "city": city,
                "country_code": country,
                "description": body,
            })
    return records


class LucyRailtonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="lucyrailton_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)"})
        records = []
        for url in ARCHIVE_URLS:
            try:
                log_message("Fetching performance archive", event="crawler_url_fetch", url=url)
                response = session.get(url, timeout=30)
                response.raise_for_status()
                records.extend(_parse_page(response.text, url))
            except requests.RequestException as error:
                log_message(
                    "Performance archive fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message("Performance archive parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    LucyRailtonCrawler().run()


if __name__ == "__main__":
    main()
