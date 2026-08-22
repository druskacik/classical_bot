import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Hermann Nitsch"
SOURCE_URL = "https://www.nitsch.org/action_type/music/"
USER_AGENT = (
    "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0; "
    "+https://github.com/)"
)


def _text(cell) -> str:
    value = cell.select_one(".tvalue")
    return value.get_text("\n", strip=True) if value else cell.get_text("\n", strip=True)


def _parse_date(value: str) -> str | None:
    """Accept only dates for which the source publishes an exact day."""
    if not re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", value):
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _description(cells) -> str | None:
    parts = []
    for label, index in (
        ("Thema", 3),
        ("Dauer", 7),
        ("Dirigent/Leitung", 8),
        ("Orchester/Musiker", 9),
    ):
        value = _text(cells[index])
        if value:
            parts.append(f"{label}: {value}")
    details = cells[3].select_one(".moreinfo")
    if details:
        detail_text = details.get_text("\n", strip=True)
        if detail_text:
            parts.append(detail_text)
    return "\n".join(parts) or None


class NitschOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nitsch_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert catalogue", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(
            SOURCE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        skipped_count = 0
        table = soup.select_one("table.sortable")
        if table is None:
            raise ValueError("Concert catalogue table was not found")

        for row in table.select("tr")[1:]:
            cells = row.find_all("td", recursive=False)
            if len(cells) < 10:
                skipped_count += 1
                continue

            title = _text(cells[0])
            event_date = _parse_date(_text(cells[1]))
            city = _text(cells[4])
            venue = _text(cells[5])
            country_code = _text(cells[6]).upper()
            if not all((title, event_date, city, venue)) or not re.fullmatch(
                r"[A-Z]{2}", country_code
            ):
                skipped_count += 1
                continue

            link = cells[0].find("a", href=True)
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": link["href"] if link else SOURCE_URL,
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": _description(cells),
                }
            )

        log_message(
            "Parsed concert catalogue",
            event="crawler_parse_completed",
            url=SOURCE_URL,
            record_count=len(records),
            skipped_count=skipped_count,
        )
        return records


def main():
    NitschOrgCrawler().run()


if __name__ == "__main__":
    main()
