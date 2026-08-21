import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://howardshore.com/"
SOURCE = "Howard Shore"
CATEGORY_URLS = (
    "https://howardshore.com/category/news/",
    "https://howardshore.com/category/archive/",
)
HEADERS = {"User-Agent": "classical-concert-crawler/1.0"}
MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
DATE_RE = re.compile(
    rf"\b({MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(\d{{4}})\b",
    re.IGNORECASE,
)
RANGE_START_RE = re.compile(
    rf"\b({MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*[–—-]\s*"
    rf"(?:({MONTHS})\.?\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,)?\s+(\d{{4}})\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*([ap])\.?m\.?\b", re.IGNORECASE)
VENUE_RE = re.compile(
    r"\b(hall|arena|theat(?:re|er)|auditorium|centre|center|opera|cathedral|"
    r"church|chapel|philharmoni|konzerthaus|concertgebouw|stadium|pavilion|"
    r"amphitheat(?:re|er)|arts centre|arts center)\b",
    re.IGNORECASE,
)

COUNTRIES = {
    "australia": "AU", "austria": "AT", "belgium": "BE", "brazil": "BR",
    "canada": "CA", "china": "CN", "czech republic": "CZ", "czechia": "CZ",
    "denmark": "DK", "finland": "FI", "france": "FR", "germany": "DE",
    "ireland": "IE", "italy": "IT", "japan": "JP", "netherlands": "NL",
    "new zealand": "NZ", "norway": "NO", "poland": "PL", "portugal": "PT",
    "singapore": "SG", "slovakia": "SK", "spain": "ES", "sweden": "SE",
    "switzerland": "CH", "united kingdom": "GB", "uk": "GB",
    "united states": "US", "usa": "US",
}
CITY_COUNTRIES = {
    "amsterdam": "NL", "ann arbor": "US", "atlantic city": "US", "austin": "US",
    "berlin": "DE", "birmingham": "GB", "brno": "CZ", "brussels": "BE",
    "calgary": "CA", "cologne": "DE", "dublin": "IE", "edmonton": "CA",
    "glasgow": "GB", "hamburg": "DE", "london": "GB", "los angeles": "US",
    "manchester": "GB", "melbourne": "AU", "montreal": "CA", "new york": "US",
    "oberhausen": "DE", "ottawa": "CA", "paris": "FR", "pittsburgh": "US",
    "prague": "CZ", "quebec": "CA", "rotterdam": "NL", "sydney": "AU",
    "toronto": "CA", "vancouver": "CA", "vienna": "AT", "winnipeg": "CA",
}
US_STATE_RE = re.compile(
    r"(?:,|\b)(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|"
    r"MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|"
    r"TN|TX|UT|VT|VA|WA|WV|WI|WY)(?:\b|,)", re.IGNORECASE
)


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _event_urls(session):
    urls = []
    for category_url in CATEGORY_URLS:
        for page in range(1, 101):
            url = category_url if page == 1 else urljoin(category_url, f"page/{page}/")
            response = session.get(url, timeout=45)
            if response.status_code == 404:
                break
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            page_urls = [
                heading["href"]
                for heading in soup.select("#content h3.page-title a[href]")
            ]
            if not page_urls:
                break
            urls.extend(page_urls)
    return list(dict.fromkeys(urls))


def _location(title, description):
    prefix = re.search(rf"[–—:]\s*(.+?)(?=\s*[-–—,]?\s*(?:{MONTHS})\b)", title, re.I)
    location = _clean(prefix.group(1).strip(" ,-–—")) if prefix else ""
    if not location:
        return None, None
    city = _clean(location.split(",", 1)[0])
    evidence = f"{location} {description}"
    lowered = evidence.lower()
    country = next((code for name, code in COUNTRIES.items() if re.search(rf"\b{re.escape(name)}\b", lowered)), None)
    country = country or CITY_COUNTRIES.get(city.lower())
    if country is None and US_STATE_RE.search(location):
        country = "US"
    return city or None, country


def _dates_and_times(text):
    occurrences = []
    # A range such as "September 30 – October 1, 2026" supplies the year only
    # once. Preserve its otherwise-hidden first occurrence.
    for match in RANGE_START_RE.finditer(text):
        raw = re.sub(r"\bSept\b", "Sep", f"{match.group(1)} {match.group(2)} {match.group(4)}")
        for date_format in ("%B %d %Y", "%b %d %Y"):
            try:
                occurrences.append((datetime.strptime(raw, date_format).date().isoformat(), None))
                break
            except ValueError:
                pass
    for match in DATE_RE.finditer(text):
        raw = f"{match.group(1)} {match.group(2)} {match.group(3)}"
        try:
            date = datetime.strptime(re.sub(r"\bSept\b", "Sep", raw), "%B %d %Y").date()
        except ValueError:
            try:
                date = datetime.strptime(re.sub(r"\bSept\b", "Sep", raw), "%b %d %Y").date()
            except ValueError:
                continue
        nearby = text[match.end():match.end() + 35]
        time_match = TIME_RE.search(nearby)
        time_from = None
        if time_match:
            parsed = datetime.strptime(f"{time_match.group(1)} {time_match.group(2)}m", "%I:%M %p")
            time_from = parsed.strftime("%H:%M")
        occurrences.append((date.isoformat(), time_from))
    return list(dict.fromkeys(occurrences))


def _parse_detail(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    article = soup.select_one("div.type-post div.post-article")
    title_node = article.select_one("h1.page-title") if article else None
    if not article or not title_node:
        return []
    title = _clean(title_node.get_text(" ", strip=True))
    content_nodes = [
        node for node in article.find_all(["p", "ul", "ol", "h2", "h3"], recursive=False)
        if "related-posts" not in (node.get("class") or [])
    ]
    lines = [
        _clean(part)
        for node in content_nodes
        for part in node.get_text("\n", strip=True).splitlines()
        if _clean(part)
    ]
    description = "\n".join(lines) or None
    city, country_code = _location(title, description or "")
    venue = next((line for line in reversed(lines) if VENUE_RE.search(line) and len(line) <= 180), None)
    if not city or not country_code or not venue:
        return []
    records = []
    for date, time_from in _dates_and_times(description or ""):
        records.append({
            "title": title,
            "date": date,
            "url": url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        })
    return records


class HowardShoreCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="howardshore_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = _event_urls(session)
        records = []
        for url in urls:
            try:
                records.extend(_parse_detail(session, url))
            except requests.RequestException as error:
                log_message(
                    "Concert detail request failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message("Scrape completed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    HowardShoreCrawler().run()


if __name__ == "__main__":
    main()
