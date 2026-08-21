import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://edwardgregson.com/"
SOURCE = "Edward Gregson"
ARCHIVE_URL = urljoin(SOURCE_URL, "performances/archive/")

COUNTRY_CODES = {
    "argentina": "AR", "australia": "AU", "austria": "AT", "belgium": "BE",
    "brazil": "BR", "canada": "CA", "china": "CN", "czech republic": "CZ",
    "czechia": "CZ", "denmark": "DK", "finland": "FI", "france": "FR",
    "germany": "DE", "hong kong": "HK", "hungary": "HU", "ireland": "IE",
    "italy": "IT", "japan": "JP", "luxembourg": "LU", "netherlands": "NL",
    "new zealand": "NZ", "norway": "NO", "peru": "PE", "poland": "PL",
    "portugal": "PT", "singapore": "SG", "south africa": "ZA", "south korea": "KR",
    "spain": "ES", "sweden": "SE", "switzerland": "CH", "taiwan": "TW",
    "united kingdom": "GB", "uk": "GB", "united states": "US", "usa": "US",
}
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia",
}
UK_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)
POSTCODE_ONLY_RE = re.compile(r"^(?:[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}|\d{4,6}(?:-\d{4})?)$", re.I)
DATE_RE = re.compile(
    r"^(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})(?:,?\s+(?P<time>\d{1,2}:\d{2}\s*[ap]m))?",
    re.I,
)


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip(" ,")


def _country_code(address_lines):
    joined = " ".join(address_lines)
    for name, code in COUNTRY_CODES.items():
        if re.search(rf"\b{re.escape(name)}\b", joined, re.I):
            return code
    if UK_POSTCODE_RE.search(joined):
        return "GB"
    if any(re.search(rf"\b{state}\s+\d{{5}}(?:-\d{{4}})?\b", joined) for state in US_STATES):
        return "US"
    return None


def _city(address_lines, country_code):
    lines = list(address_lines)
    if lines and _clean(lines[-1]).lower() in COUNTRY_CODES:
        lines.pop()
    if country_code == "US":
        for index, line in enumerate(lines):
            if re.search(r"\b(?:" + "|".join(US_STATES) + r")\s+\d{5}(?:-\d{4})?\b", line):
                if index:
                    city_index = index - 1
                    if lines[city_index].lower() in US_STATE_NAMES and city_index:
                        city_index -= 1
                    return _clean(lines[city_index].split(",")[-1])
    if country_code == "GB":
        for index, line in enumerate(lines):
            if UK_POSTCODE_RE.search(line):
                candidates = lines[1:index]
                if candidates:
                    return _clean(candidates[0])
    for line in reversed(lines[1:]):
        candidate = _clean(re.sub(r"\b\d{4,6}(?:-\d{4})?\b", "", line))
        candidate = _clean(candidate.split(",")[0])
        if candidate and not POSTCODE_ONLY_RE.fullmatch(candidate) and not re.search(r"\d", candidate):
            return candidate
    return None


class EdwardGregsonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="edwardgregson_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "classical-concert-crawler/1.0"})

    def _get_soup(self, url):
        log_message("Fetching page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def _event_urls(self):
        current_year = date.today().year
        urls = set()
        for year in range(2005, current_year + 2):
            soup = self._get_soup(f"{ARCHIVE_URL}?yr={year}")
            urls.update(
                urljoin(SOURCE_URL, link["href"])
                for link in soup.select('a[href*="/concerts/event/"]')
            )
        return sorted(urls)

    def _parse_event(self, url):
        soup = self._get_soup(url)
        title_nodes = soup.select("h1")
        title_node = title_nodes[1] if len(title_nodes) > 1 else None
        blocks = [block for block in soup.select(".fl-rich-text") if block.get_text(" ", strip=True)]
        details = None
        details_block = None
        for block in blocks:
            strings = [_clean(text) for text in block.stripped_strings if _clean(text)]
            if strings and DATE_RE.match(strings[0]):
                details = strings
                details_block = block
                break
        if not title_node or not details or len(details) < 2:
            return None

        match = DATE_RE.match(details[0])
        event_date = datetime.strptime(match.group("date"), "%B %d, %Y").date().isoformat()
        time_from = None
        if match.group("time"):
            time_from = datetime.strptime(match.group("time").replace(" ", ""), "%I:%M%p").time().isoformat()

        venue = _clean(details[1])
        address = details[2:]
        country_code = _country_code(address)
        city = _city(address, country_code) if country_code else None
        if not venue or venue.lower() == "various venues" or not city or not country_code:
            log_message(
                "Skipping event with unresolved location",
                event="crawler_record_skipped",
                url=url,
                venue=venue or None,
                city=city,
                country_code=country_code,
            )
            return None

        description_parts = []
        for block in blocks:
            text = _clean(block.get_text("\n", strip=True))
            if text and block is not details_block and text not in description_parts:
                description_parts.append(text)
        description = "\n\n".join(description_parts) or None
        return {
            "title": _clean(title_node.get_text(" ", strip=True)),
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }

    def scrape(self):
        event_urls = self._event_urls()
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._parse_event, url): url for url in event_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        "Event detail could not be parsed",
                        event="crawler_url_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        records.sort(key=lambda record: (record["date"], record["time_from"] or "", record["url"]))
        return records


def main():
    EdwardGregsonCrawler().run()


if __name__ == "__main__":
    main()
