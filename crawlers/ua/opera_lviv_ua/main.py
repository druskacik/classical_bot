from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Львівська національна опера"
SOURCE_URL = "https://opera.lviv.ua/"
CALENDAR_URL = f"{SOURCE_URL}afisha/"
ARCHIVE_START = "2000-01-01"
DEFAULT_VENUES = {"головна сцена", "дзеркальна зала", "дзеркаль зала"}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _get_soup(session: requests.Session, url: str, **kwargs) -> BeautifulSoup:
    log_message("Fetching crawler page", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def _description(session: requests.Session, url: str) -> str | None:
    try:
        soup = _get_soup(session, url)
    except requests.RequestException as error:
        log_message(
            "Concert detail fetch failed",
            event="crawler_detail_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    content = soup.select_one("main.show-content")
    if content is None:
        return None
    text = content.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text or None


def _location(venue: str, description: str | None) -> tuple[str, str] | None:
    normalized = _clean_text(venue).casefold()
    if normalized in DEFAULT_VENUES:
        return venue, "Львів"

    evidence = description or ""
    if "Жовків" in venue or re.search(r"\bм\.\s*Жовква\b", evidence, re.IGNORECASE):
        return venue, "Жовква"

    return None


class OperaLvivCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="opera_lviv_ua",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="UA",
        upload_target="potential",
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update({"User-Agent": "classical-concert-crawler/1.0"})
        soup = _get_soup(
            session,
            CALENDAR_URL,
            params={"tribe-bar-date": ARCHIVE_START},
        )

        candidates = []
        for row in soup.select(".tribe-events-calendar-list__custom-row"):
            time_element = row.select_one("time[datetime]")
            title_link = row.select_one("h2.show-title")
            if time_element is None or title_link is None or title_link.parent is None:
                continue

            details = [
                _clean_text(element.get_text(" ", strip=True))
                for element in row.select(".show-details-item")
            ]
            if len(details) < 3:
                continue

            try:
                start = datetime.strptime(
                    time_element["datetime"], "%Y-%m-%d %H:%M:%S"
                )
            except (KeyError, ValueError):
                continue

            title = _clean_text(title_link.get_text(" ", strip=True))
            url = title_link.parent.get("href")
            venue = details[2]
            if not title or not url or not venue:
                continue

            candidates.append((title, start, url, venue))

        detail_urls = list(dict.fromkeys(candidate[2] for candidate in candidates))
        with ThreadPoolExecutor(max_workers=8) as executor:
            descriptions = executor.map(
                lambda detail_url: _description(session, detail_url), detail_urls
            )
        description_cache = dict(zip(detail_urls, descriptions))

        records = []
        for title, start, url, venue in candidates:
            description = description_cache[url]
            location = _location(venue, description)
            if location is None:
                log_message(
                    "Skipping event with unresolved location",
                    event="crawler_event_skipped",
                    url=url,
                    venue=venue,
                )
                continue

            venue, city = location
            records.append(
                {
                    "title": title,
                    "date": start.date().isoformat(),
                    "url": url,
                    "time_from": start.time().strftime("%H:%M"),
                    "venue": venue,
                    "city": city,
                    "description": description,
                }
            )

        log_message(
            "Calendar parsed",
            event="crawler_calendar_parsed",
            url=CALENDAR_URL,
            record_count=len(records),
        )
        return records


def main():
    OperaLvivCrawler().run()


if __name__ == "__main__":
    main()
