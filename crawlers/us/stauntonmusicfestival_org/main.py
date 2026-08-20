import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Staunton Music Festival"
SOURCE_URL = "https://www.stauntonmusicfestival.org/"
SITEMAP_URL = f"{SOURCE_URL}sitemap.xml"
REQUEST_TIMEOUT = 30

DATE_RE = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"(?P<date>[A-Z][a-z]{2} \d{1,2}, \d{4}) at (?P<time>\d{1,2}:\d{2} [ap]m)$"
)
VENUE_SKIP_RE = re.compile(
    r"^(?:doors? open|free preconcert talk|preconcert talk|food and drink|"
    r"concert takes place|all seating)",
    re.IGNORECASE,
)
INVALID_VENUE_RE = re.compile(
    r"(?:provided|details).*?(?:after|later)|(?:venue|location).*?(?:tba|tbd)",
    re.IGNORECASE,
)


def _get(url: str) -> requests.Response:
    log_message("Fetching crawler URL", event="crawler_url_fetch", url=url)
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
    )
    response.raise_for_status()
    return response


def _event_sitemap_urls() -> list[str]:
    root = ElementTree.fromstring(_get(SITEMAP_URL).content)
    sitemap_urls = [node.text for node in root.findall(".//{*}loc") if node.text]
    event_sitemaps = [url for url in sitemap_urls if "dynamic-events_" in url]
    if not event_sitemaps:
        raise ValueError("The Wix event sitemap was not found")

    event_urls: list[str] = []
    for sitemap_url in event_sitemaps:
        sitemap = ElementTree.fromstring(_get(sitemap_url).content)
        event_urls.extend(
            node.text
            for node in sitemap.findall(".//{*}loc")
            if node.text and "/events/" in node.text
        )
    return list(dict.fromkeys(event_urls))


def _clean_lines(soup: BeautifulSoup) -> list[str]:
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    return [
        line
        for raw_line in soup.get_text("\n").splitlines()
        if (line := re.sub(r"\s+", " ", raw_line).strip().strip("\u200b"))
    ]


def _parse_event(url: str) -> dict | None:
    soup = BeautifulSoup(_get(url).content, "html.parser")
    title_meta = soup.select_one('meta[property="og:title"]')
    description_meta = soup.select_one('meta[property="og:description"]')
    title = title_meta.get("content", "").strip() if title_meta else ""
    if not title:
        log_message("Skipping event without title", event="crawler_record_skipped", url=url)
        return None

    lines = _clean_lines(soup)
    title_indexes = [index for index, line in enumerate(lines) if line == title]
    start = title_indexes[-1] + 1 if title_indexes else 0

    date_index = None
    date_match = None
    for index in range(start, min(start + 15, len(lines))):
        if match := DATE_RE.match(lines[index]):
            date_index = index
            date_match = match
            break
    if date_index is None or date_match is None:
        log_message("Skipping event without parseable date", event="crawler_record_skipped", url=url)
        return None

    venue = None
    for line in lines[date_index + 1 : date_index + 8]:
        if VENUE_SKIP_RE.match(line):
            continue
        venue = line
        break
    if not venue or INVALID_VENUE_RE.search(venue):
        log_message("Skipping event without a concrete venue", event="crawler_record_skipped", url=url)
        return None

    parsed_date = datetime.strptime(date_match.group("date"), "%b %d, %Y").date()
    parsed_time = datetime.strptime(date_match.group("time"), "%I:%M %p").time()
    description = description_meta.get("content", "").strip() if description_meta else ""

    return {
        "title": title,
        "date": parsed_date.isoformat(),
        "url": url,
        "time_from": parsed_time.strftime("%H:%M:%S"),
        "time_to": None,
        "venue": venue,
        "city": "Staunton",
        "country_code": "US",
        "description": description or None,
        "source_url": SOURCE_URL,
        "source": SOURCE,
    }


class StauntonMusicFestivalCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="stauntonmusicfestival_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        event_urls = _event_sitemap_urls()
        records: list[dict] = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_parse_event, url): url for url in event_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    if record := future.result():
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        "Event detail request failed",
                        event="crawler_url_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records.sort(key=lambda record: (record["date"], record["time_from"], record["url"]))
        log_message(
            "Staunton Music Festival scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    StauntonMusicFestivalCrawler().run()


if __name__ == "__main__":
    main()
