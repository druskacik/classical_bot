import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://concert.lisztacademy.hu/"
SOURCE = "Liszt Academy"
PROGRAMS_URL = urljoin(SOURCE_URL, "programs")

DATE_RANGE = "2000-01-01 to 2100-12-31"
TIME_RE = re.compile(r"\b(\d{1,2})[.:](\d{2})(?:\s*[-–]\s*(\d{1,2})[.:](\d{2}))?")
DATE_IN_URL_RE = re.compile(r"/programs/(\d{4}-\d{2}-\d{2})-")


class LisztAcademyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="concert_lisztacademy_hu",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="HU",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "classical-concert-crawler/1.0"})

    def _get_soup(self, url, params=None):
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def _listing_urls(self):
        page = 1
        seen = set()
        while True:
            params = {
                "page": page,
                "mufaj": "",
                "tipus": "",
                "helyszin": "",
                "limit": 100,
                "idopont": DATE_RANGE,
            }
            soup = self._get_soup(PROGRAMS_URL, params=params)
            urls = {
                urljoin(SOURCE_URL, anchor["href"])
                for anchor in soup.select('a[href*="/programs/"]')
                if DATE_IN_URL_RE.search(anchor.get("href", ""))
            }
            new_urls = urls - seen
            if not new_urls:
                break
            seen.update(new_urls)
            yield from sorted(new_urls)

            next_link = next(
                (a for a in soup.select("a.btn[href]") if a.get_text(" ", strip=True).upper() == "NEXT"),
                None,
            )
            if next_link is None:
                break
            page += 1

    def _parse_detail(self, url):
        try:
            soup = self._get_soup(url)
            event = soup.select_one("article.event")
            if event is None:
                return None

            title_node = event.select_one("h1")
            if title_node is None:
                return None
            title_node = BeautifulSoup(str(title_node), "html.parser")
            for node in title_node.select(".self-event"):
                node.decompose()
            title = " ".join(title_node.get_text(" ", strip=True).split())

            match = DATE_IN_URL_RE.search(url)
            if not match:
                return None
            event_date = match.group(1)
            date.fromisoformat(event_date)

            metadata = event.select(".img-subtitle p.event-bold")
            if len(metadata) < 2:
                return None
            date_time_text = metadata[0].get_text(" ", strip=True)
            venue = " ".join(metadata[1].get_text(" ", strip=True).split())
            if not title or not venue:
                return None

            time_match = TIME_RE.search(date_time_text)
            time_from = None
            time_to = None
            if time_match:
                time_from = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
                if time_match.group(3):
                    time_to = f"{int(time_match.group(3)):02d}:{time_match.group(4)}"

            description_node = event.select_one(".content-wrapper > .content.icms-content")
            description = None
            if description_node:
                description = description_node.get_text("\n", strip=True)
                description = re.sub(r"\n{3,}", "\n\n", description).strip() or None

            return {
                "title": title,
                "date": event_date,
                "url": url,
                "time_from": time_from,
                "time_to": time_to,
                "venue": venue,
                "city": "Budapest",
                "description": description,
            }
        except (requests.RequestException, ValueError) as error:
            log_message(
                "Failed to parse concert detail",
                event="crawler_url_failed",
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

    def scrape(self):
        urls = set()
        try:
            urls.update(self._listing_urls())
        except requests.RequestException as error:
            log_message(
                "Failed to fetch event listing",
                event="crawler_url_failed",
                url=PROGRAMS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._parse_detail, url): url for url in urls}
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)
        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["title"]))
        return records


def main():
    LisztAcademyCrawler().run()


if __name__ == "__main__":
    main()
