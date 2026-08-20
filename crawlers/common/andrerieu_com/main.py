import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "André Rieu"
SOURCE_URL = "https://andrerieu.com/en"
TOUR_URL = "https://andrerieu.com/en/tour"
REQUEST_TIMEOUT = 30

COUNTRY_CODES = {
    "Austria": "AT",
    "Bahrain": "BH",
    "Belgium": "BE",
    "Croatia": "HR",
    "Czech Republic": "CZ",
    "Finland": "FI",
    "France": "FR",
    "Germany": "DE",
    "Hungary": "HU",
    "Latvia": "LV",
    "Lithuania": "LT",
    "Netherlands": "NL",
    "Poland": "PL",
    "Portugal": "PT",
    "Serbia": "RS",
    "Slovakia": "SK",
    "Slovenia": "SI",
    "Switzerland": "CH",
    "The Netherlands": "NL",
    "United Kingdom": "GB",
}

COUNTRY_SLUG_CODES = {
    "austria": "AT", "bahrain": "BH", "belgium": "BE", "croatia": "HR",
    "czech-republic": "CZ", "finland": "FI", "france": "FR", "germany": "DE",
    "hungary": "HU", "latvia": "LV", "lithuania": "LT", "netherlands": "NL",
    "the-netherlands": "NL", "poland": "PL", "polen": "PL", "portugal": "PT",
    "serbia": "RS", "slovakia": "SK", "slovenia": "SI", "switzerland": "CH",
    "uk": "GB",
}

MONTHS = {
    name: number for number, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"),
        start=1,
    )
}


def clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def fetch_soup(url):
    log_message("Fetching page", event="crawler_url_fetch", url=url)
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
    )
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def parse_detail(url, city, venue):
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    date_match = re.search(
        r"-(january|february|march|april|may|june|july|august|september|october|november|december)-(\d{1,2})(?:st|nd|rd|th)?-(\d{4})(?:-|$)",
        slug,
    )
    if date_match is None:
        # One current Polish URL uses the localized ordering "12-mei-2027".
        date_match = re.search(r"-(\d{1,2})-mei-(\d{4})(?:-|$)", slug)
        if date_match is None:
            raise ValueError("date is missing from concert URL")
        date = f"{int(date_match.group(2)):04d}-05-{int(date_match.group(1)):02d}"
    else:
        date = f"{int(date_match.group(3)):04d}-{MONTHS[date_match.group(1)]:02d}-{int(date_match.group(2)):02d}"

    country_code = next(
        (code for suffix, code in COUNTRY_SLUG_CODES.items() if slug.endswith(f"-{suffix}") or slug == suffix),
        "BH" if "bahrain" in slug or "bahrain" in city.casefold() else None,
    )
    if country_code is None:
        raise ValueError("country is missing from concert URL")

    record = {
        "title": f"André Rieu: {city} - {venue}",
        "date": date,
        "url": url,
        "time_from": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": None,
    }

    concert = None
    soup = None
    for attempt in range(3):
        soup = fetch_soup(url)
        concert = soup.select_one(".concert-info")
        if concert is not None:
            break
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    if concert is None:
        return record

    schema = soup.select_one('script[type="application/ld+json"]')
    schema_text = schema.get_text(" ", strip=True) if schema else ""
    start_match = re.search(
        r'"startDate"\s*:\s*"(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}))?',
        schema_text,
    )
    if not start_match:
        return record

    displayed_title = clean_text(concert.select_one("h1").get_text(" ", strip=True))
    title = f"André Rieu: {displayed_title or city}"

    address = concert.select_one(".spec-venue p p") or concert.select_one(".spec-venue p")
    address_parts = list(address.stripped_strings) if address else []
    country_name = clean_text(address_parts[-1]) if address_parts else None
    detail_country_code = COUNTRY_CODES.get(country_name)
    if detail_country_code:
        country_code = detail_country_code

    time_node = concert.select_one(".spec-time p")
    time_from = clean_text(time_node.get_text(" ", strip=True)) if time_node else None
    if time_from and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_from):
        time_from = None

    content = concert.select_one(".main-content")
    paragraphs = [clean_text(node.get_text(" ", strip=True)) for node in content.select("p")] if content else []
    description = "\n\n".join(text for text in paragraphs if text) or None

    return {
        "title": title,
        "date": start_match.group(1),
        "url": url,
        "time_from": time_from or start_match.group(2),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


def get_detail_stubs(soup):
    stubs = []
    seen = set()
    for item in soup.select("ul.concerts li"):
        link = item.select_one('a[href^="/en/tour/"]')
        city_node = item.select_one(".city")
        venue_node = item.select_one(".location")
        if not (link and city_node and venue_node):
            continue
        url = urljoin(SOURCE_URL, link.get("href"))
        city = clean_text(city_node.get_text(" ", strip=True))
        venue = clean_text(venue_node.get_text(" ", strip=True))
        if url in seen or not city or not venue or venue.casefold() == city.casefold():
            continue
        seen.add(url)
        stubs.append((url, city, venue))
    return stubs


class AndreRieuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="andrerieu_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date"],
    )

    def scrape(self):
        # The home page includes the same complete server-rendered tour list as
        # /en/tour, plus a stable internal detail link for every ticket state.
        stubs = get_detail_stubs(fetch_soup(SOURCE_URL))
        records = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(parse_detail, url, city, venue): url
                for url, city, venue in stubs
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.append(future.result())
                except Exception as error:
                    log_message(
                        "Skipping invalid concert detail",
                        event="crawler_record_skipped",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        records.sort(key=lambda record: (record["date"], record["time_from"] or "", record["url"]))
        return records


def main():
    AndreRieuCrawler().run()


if __name__ == "__main__":
    main()
