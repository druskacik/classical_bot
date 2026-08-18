import re
from datetime import datetime
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Burbank Philharmonic Orchestra"
SOURCE_URL = "https://www.burbankphilharmonic.org/"
SITEMAP_URL = f"{SOURCE_URL}pages-sitemap.xml"
TIMEOUT = 30

DATE_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?\s*,\s*(20\d{2})\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.IGNORECASE)
CITY_RE = re.compile(r"\b([A-Za-z][A-Za-z .'-]+),\s*CA\s+\d{5}(?:-\d{4})?\b")
ADDRESS_RE = re.compile(r"\b\d+\s+.+(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?)\b", re.I)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _sitemap_urls(session: requests.Session) -> list[str]:
    log_message("Fetching sitemap", event="crawler_url_fetch", url=SITEMAP_URL)
    response = session.get(SITEMAP_URL, timeout=TIMEOUT)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    return [node.text.strip() for node in root.findall(".//{*}loc") if node.text]


def _parse_event(url: str, html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main", id="PAGES_CONTAINER") or soup.find("main")
    if main is None:
        return None

    lines = [_clean(text) for text in main.stripped_strings]
    lines = [line for line in lines if line and line != "\u200b"]
    if not lines:
        return None

    heading = main.find(["h1", "h2"])
    title = _clean(heading.get_text(" ", strip=True)) if heading else lines[0]
    page_text = "\n".join(lines)
    if "concert" not in f"{title} {page_text}".lower():
        return None

    date_match = DATE_RE.search(page_text)
    time_match = TIME_RE.search(page_text)
    city_match = CITY_RE.search(page_text)
    if not (date_match and time_match and city_match):
        return None

    try:
        event_date = datetime.strptime(
            f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}", "%B %d %Y"
        ).date().isoformat()
    except ValueError:
        return None

    hour = int(time_match.group(1)) % 12
    if time_match.group(3).lower() == "p":
        hour += 12
    time_from = f"{hour:02d}:{int(time_match.group(2) or 0):02d}"
    city = _clean(city_match.group(1))

    date_line_index = next((i for i, line in enumerate(lines) if DATE_RE.search(line)), -1)
    venue = None
    if date_line_index >= 0:
        for line in lines[date_line_index + 1 : date_line_index + 6]:
            if TIME_RE.fullmatch(line) or CITY_RE.search(line) or ADDRESS_RE.search(line):
                continue
            if len(line) <= 100:
                venue = line
                break
    if not venue:
        return None

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "description": page_text,
    }


class BurbankPhilharmonicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="burbankphilharmonic_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalBot/1.0)"
        records = []
        for url in _sitemap_urls(session):
            log_message("Fetching page", event="crawler_url_fetch", url=url)
            try:
                response = session.get(url, timeout=TIMEOUT)
                response.raise_for_status()
                record = _parse_event(response.url, response.text)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    "Page fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message("Sitemap scrape completed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    BurbankPhilharmonicCrawler().run()


if __name__ == "__main__":
    main()
