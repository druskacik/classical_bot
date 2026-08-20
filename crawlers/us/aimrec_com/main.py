import html
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.aimrec.com/"
SOURCE = "AIMREC"
SITEMAP_URL = urljoin(SOURCE_URL, "sitemap.xml")
MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
DATE_RE = re.compile(rf"\b({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})\b", re.I)
TIME_RE = re.compile(
    r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?"
    r"(?:\s+(?:EST|EDT|Eastern(?: Standard)? Time))?\b",
    re.I,
)
ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+.+?,\s*([A-Za-z .'-]+),\s*(?:Maryland|MD)\s+\d{5}(?:-\d{4})?\b",
    re.I,
)


class AimrecCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="aimrec_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "classical-concert-crawler/1.0"})

    def _get(self, url, **kwargs):
        log_message("Fetching AIMREC page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=30, **kwargs)
        response.raise_for_status()
        return response

    def _news_urls(self):
        soup = BeautifulSoup(self._get(SITEMAP_URL).content, "xml")
        return [
            loc.get_text(strip=True)
            for loc in soup.find_all("loc")
            if "/latest-news/" in loc.get_text()
            and "/tag/" not in loc.get_text()
            and "/category/" not in loc.get_text()
        ]

    @staticmethod
    def _record_from_item(url, item):
        body = BeautifulSoup(item.get("body") or "", "html.parser")
        paragraphs = [p.get_text(" ", strip=True) for p in body.find_all(["p", "h2", "h3"])]
        description = "\n".join(dict.fromkeys(p for p in paragraphs if p))

        # Online-only streams are outside project scope, while undated tour
        # announcements are not concrete occurrences.
        if re.search(r"\b(?:online|live stream|streaming)\b", description, re.I):
            return None
        date_match = DATE_RE.search(description)
        address_match = ADDRESS_RE.search(description)
        if not date_match or not address_match:
            return None

        date_value = datetime.strptime(" ".join(date_match.groups()), "%B %d %Y").date().isoformat()
        city = address_match.group(1).strip()
        address_paragraph_index = next(
            (i for i, paragraph in enumerate(paragraphs) if ADDRESS_RE.search(paragraph)), None
        )
        if address_paragraph_index is None or address_paragraph_index == 0:
            return None
        venue = paragraphs[address_paragraph_index - 1].strip()
        if (
            not venue
            or venue.casefold() == city.casefold()
            or re.search(r"\b(?:ticket|\$\d)", venue, re.I)
        ):
            return None

        time_match = TIME_RE.search(description[date_match.end():]) or TIME_RE.search(description)
        time_from = None
        if time_match:
            hour = int(time_match.group(1)) % 12
            if time_match.group(3).lower() == "p":
                hour += 12
            time_from = f"{hour:02d}:{int(time_match.group(2) or 0):02d}"

        return {
            "title": html.unescape(item.get("title") or "").strip(),
            "date": date_value,
            "url": url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "description": description or None,
        }

    def scrape(self):
        records = []
        for url in self._news_urls():
            try:
                payload = self._get(f"{url}?format=json").json()
                item = payload.get("item") or next(iter(payload.get("items") or []), None)
                if item:
                    record = self._record_from_item(url, item)
                    required_fields = ("title", "date", "url", "venue", "city")
                    if record and all(record.get(field) for field in required_fields):
                        records.append(record)
            except (requests.RequestException, ValueError, TypeError) as error:
                log_message(
                    "Skipping unreadable AIMREC news item",
                    event="crawler_item_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return records


def main():
    AimrecCrawler().run()


if __name__ == "__main__":
    main()
