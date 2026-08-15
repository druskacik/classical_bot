import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Cape Symphony Orchestra"
SOURCE_URL = "https://capesymphony.org/"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar")
SYMPHONY_CATEGORY_ID = "40"
ARCHIVE_START_YEAR = 2022
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


class CapeSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="capesymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _get_soup(self, url, *, params=None):
        log_message("Fetching Cape Symphony page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, params=params, timeout=45)
        response.raise_for_status()
        return BeautifulSoup(response.content, "html.parser")

    @staticmethod
    def _clean_text(node):
        if node is None:
            return None
        text = node.get_text("\n", strip=True)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or None

    @staticmethod
    def _season_url(year):
        return urljoin(SOURCE_URL, f"orchestra/{year}-{str(year + 1)[-2:]}-season")

    def _season_details(self):
        """Index first-party orchestra pages by title for venue/program enrichment."""
        details = {}
        # Probe one season beyond the current year so an early-announced season is included.
        for year in range(ARCHIVE_START_YEAR, date.today().year + 2):
            season_url = self._season_url(year)
            soup = self._get_soup(season_url)
            if soup.title and soup.title.get_text(" ", strip=True) == "404":
                continue
            links = {
                urljoin(SOURCE_URL, anchor["href"])
                for anchor in soup.select(f'a[href*="/orchestra/{year}-{str(year + 1)[-2:]}-season/"]')
                if anchor.get("href")
            }
            for url in sorted(links):
                event_soup = self._get_soup(url)
                event = self._parse_season_detail(event_soup, url)
                if event:
                    details[event["title"].casefold()] = event
        return details

    def _parse_season_detail(self, soup, url):
        article = soup.select_one("article")
        title_node = article.select_one("h1") if article else None
        if not article or not title_node:
            return None
        title = self._clean_text(title_node)

        location = None
        for strong in article.find_all("strong"):
            if strong.get_text(" ", strip=True).rstrip(":") == "Location":
                location = strong.parent
                break
        if location is None:
            entry = article.select_one(".featured-artist .field-value")
            if entry:
                for paragraph in entry.find_all("p"):
                    if "Location:" in paragraph.get_text(" ", strip=True):
                        location = paragraph
                        break
        if location is None:
            return None
        lines = [line.strip() for line in location.get_text("\n", strip=True).splitlines() if line.strip()]
        lines = [line for line in lines if line.rstrip(":") != "Location"]
        if not lines:
            return None
        venue = lines[0]
        city = None
        for line in lines[1:]:
            match = re.search(r"([^,]+),\s*MA\s+\d{5}", line)
            if match:
                city = match.group(1).strip()
                break
        if not city:
            return None

        body = article.select_one(".article-body, .item-page, [itemprop='articleBody']") or article
        return {
            "title": title,
            "url": url,
            "venue": venue,
            "city": city,
            "description": self._clean_text(body),
        }

    def _calendar_occurrence_links(self):
        links = set()
        # The public JCal archive begins with the 2022/23 orchestra season.
        start = datetime(ARCHIVE_START_YEAR, 7, 1)
        end = datetime(date.today().year + 2, 7, 1)
        cursor = start
        while cursor < end:
            soup = self._get_soup(
                CALENDAR_URL,
                params={"filter_catid": SYMPHONY_CATEGORY_ID, "date": cursor.strftime("%Y-%m-01")},
            )
            for anchor in soup.select(f'a[href*="/calendar/{SYMPHONY_CATEGORY_ID}-symphony-concerts/"]'):
                href = anchor.get("href")
                if href:
                    links.add(urljoin(SOURCE_URL, href.split("?", 1)[0]))
            cursor = datetime(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        return sorted(links)

    def _parse_occurrence(self, soup):
        event = soup.select_one(".jcal_event[itemtype='https://schema.org/Event'], .jcal_event")
        if event is None:
            return None
        title = self._clean_text(event.select_one("h1[itemprop='name'], h1"))
        start = event.select_one("meta[itemprop='startDate']")
        value = start.get("content") if start else None
        if not title or not value:
            return None
        try:
            occurrence = datetime.fromisoformat(value)
        except ValueError:
            return None
        return title, occurrence.date().isoformat(), occurrence.time().isoformat()

    def scrape(self):
        season_details = self._season_details()
        records = []
        for calendar_url in self._calendar_occurrence_links():
            parsed = self._parse_occurrence(self._get_soup(calendar_url))
            if not parsed:
                continue
            title, event_date, time_from = parsed
            detail = season_details.get(title.casefold())
            if not detail:
                log_message(
                    "Skipping Symphony occurrence without reliable venue detail",
                    event="crawler_record_skipped",
                    url=calendar_url,
                )
                continue
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": calendar_url,
                    "time_from": time_from,
                    "time_to": None,
                    "venue": detail["venue"],
                    "city": detail["city"],
                    "description": detail["description"],
                }
            )
        return records


def main():
    return CapeSymphonyCrawler().run()


if __name__ == "__main__":
    main()
