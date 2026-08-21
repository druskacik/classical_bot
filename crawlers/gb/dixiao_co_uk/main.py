import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Di Xiao"
SOURCE_URL = "https://dixiao.co.uk/"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _parse_date(value: str) -> str | None:
    value = _clean_text(value)
    for date_format in ("%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y"):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_time(value: str) -> str | None:
    value = _clean_text(value).lower().replace(" ", "")
    for time_format in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            return datetime.strptime(value, time_format).time().strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _venue_and_city(value: str) -> tuple[str | None, str | None]:
    value = _clean_text(value)
    parts = [part.strip() for part in re.split(r"\s+[–—-]\s+", value) if part.strip()]
    if len(parts) < 2:
        return None, None

    # The calendar uses both "hall - city" and "institution - hall" forms.
    if "birmingham" in parts[0].lower():
        return parts[-1], "Birmingham"
    return parts[0], parts[-1]


class DiXiaoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="dixiao_co_uk",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GB",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        section = soup.find("section", id="concerts")
        table = section.find("table") if section else None
        if table is None:
            log_message("Concert table not found", event="crawler_parse_warning", url=SOURCE_URL)
            return []

        records = []
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            concert_date = _parse_date(cells[0].get_text(" ", strip=True))
            venue_text = cells[1].get_text(" ", strip=True)
            venue, city = _venue_and_city(venue_text)
            link = cells[1].find("a", href=True)
            if not concert_date or not venue or not city or link is None:
                continue

            url = urljoin(SOURCE_URL, link["href"])
            records.append(
                {
                    "title": f"Di Xiao at {venue}",
                    "date": concert_date,
                    "url": url,
                    "time_from": _parse_time(cells[2].get_text(" ", strip=True)),
                    "venue": venue,
                    "city": city,
                    "description": None,
                }
            )

        log_message(
            "Concert calendar parsed",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    DiXiaoCrawler().run()


if __name__ == "__main__":
    main()
