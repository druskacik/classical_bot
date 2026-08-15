import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as calendar_date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.tso.gov.taipei/"
SOURCE = "Taipei Symphony Orchestra"
CATALOGUE_URL = urljoin(
    SOURCE_URL,
    "News_Photo.aspx?n=BC7BED8580080804&sms=8BB1830722073F63",
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/125.0 Safari/537.36"
    )
}
PERFORMANCE_RE = re.compile(
    r"(?P<month>\d{1,2})/(?P<day>\d{1,2})"
    r"(?:[（(][^）)]*[）)])?\s*"
    r"(?P<time>\d{1,2}:\d{2})\s*"
    r"(?P<venue>.+?)"
    r"(?=(?:\d{1,2}/\d{1,2})(?:[（(]|\s)|【|$)"
)


def clean_text(element):
    if element is None:
        return ""
    if hasattr(element, "get_text"):
        text = element.get_text(" ", strip=True)
    else:
        text = str(element)
    text = text.replace("\xa0", " ").replace("\u3000", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def catalogue_links(soup):
    links = []
    for anchor in soup.select('a[href*="News_Content.aspx"][href*="BC7BED8580080804"]'):
        url = urljoin(CATALOGUE_URL, anchor.get("href", ""))
        if url and url not in links:
            links.append(url)
    return links


def detail_records(soup, url):
    content = soup.select_one(".page-content")
    if content is None:
        return []
    heading = content.select_one("h1, h2, h3")
    title = clean_text(heading)
    if not title or title == "2026樂季":
        return []

    year_match = re.search(r"(?:20\d{2})", title)
    if not year_match:
        return []
    year = int(year_match.group())
    description_node = content.select_one(".essay")
    description = clean_text(description_node) or None
    if not description:
        return []

    # Concrete occurrences are published in a labelled date/time/place block.
    # Items lacking a venue (notably some salon announcements) are deliberately
    # skipped rather than assigning the orchestra's home hall to them.
    marker = re.search(r"【日期[^】]*】", description)
    if not marker:
        return []
    schedule = description[marker.end():]
    schedule = re.split(r"【(?:購票|演出者|演出曲目|票價)", schedule, maxsplit=1)[0]

    records = []
    for match in PERFORMANCE_RE.finditer(schedule):
        venue = clean_text(match.group("venue")).strip("　 ,，、")
        if not venue:
            continue
        try:
            event_date = calendar_date(
                year, int(match.group("month")), int(match.group("day"))
            ).isoformat()
        except ValueError:
            continue
        records.append({
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": match.group("time"),
            "venue": venue,
            "city": "Taipei",
            "description": description,
        })
    return records


class TsoGovTaipeiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="tso_gov_taipei",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="TW",
        upload_target="classical",
        columns=["title", "date", "url", "time_from", "venue", "city", "description"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            links = catalogue_links(fetch_soup(session, CATALOGUE_URL))
        except requests.RequestException as error:
            log_message(
                "Failed to fetch TSO concert catalogue",
                event="crawler_fetch_failed",
                level="error",
                url=CATALOGUE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_soup, session, url): url for url in links}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(detail_records(future.result(), url))
                except requests.RequestException as error:
                    log_message(
                        "Failed to fetch TSO concert detail",
                        event="crawler_detail_fetch_failed",
                        level="warning",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item["date"], item["time_from"] or "", item["title"], item["venue"]
        ))


def main():
    TsoGovTaipeiCrawler().run()


if __name__ == "__main__":
    main()
