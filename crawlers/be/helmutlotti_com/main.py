import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://helmutlotti.com/"
SOURCE = "Helmut Lotti"

COUNTRY_CODES = {
    "B": "BE",
    "BE": "BE",
    "DE": "DE",
    "NL": "NL",
}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _ticket_url(href: str | None) -> str:
    if not href:
        return SOURCE_URL

    parsed = urlparse(href)
    if parsed.netloc in {"google.com", "www.google.com"} and parsed.path == "/url":
        target = parse_qs(parsed.query).get("q")
        if target:
            return target[0]
    return href


def _location(value: str) -> tuple[str, str, str] | None:
    match = re.fullmatch(r"(.+),\s*([^,()]+)\s*\(([A-Za-z]{1,2})\)", value)
    if not match:
        return None

    venue, city, raw_country = (_clean_text(part) for part in match.groups())
    country_code = COUNTRY_CODES.get(raw_country.upper())
    if not venue or not city or not country_code:
        return None
    return venue, city, country_code


def _parse_date(value: str) -> str | None:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


class HelmutLottiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="helmutlotti_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="BE",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching tour page", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(
            SOURCE_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        table = soup.select_one("table.foo_table_9379")
        if table is None:
            table = soup.find("table", attrs={"aria-label": re.compile(r"Helmut Lotti", re.I)})
        if table is None:
            raise RuntimeError("Tour table was not found")

        records = []

        # A small "new dates" panel precedes the main tour table. It currently
        # contains one date which is not repeated in that table, so parse both
        # sources and let the configured event-key deduplication merge repeats.
        for text_node in soup.find_all(string=re.compile(r"^\s*\d{2}\.\d{2}\.\d{4}\s*$")):
            if text_node.find_parent("table") is not None:
                continue
            block = text_node.find_parent("strong")
            if block is None:
                continue
            parts = [_clean_text(part) for part in block.stripped_strings]
            date = _parse_date(parts[0]) if parts else None
            if len(parts) < 3 or not date:
                continue
            location = _location(f"{parts[1]}, {parts[2]}")
            if location is None:
                continue
            venue, city, country_code = location
            ticket = block.find("a", href=True)
            records.append(
                {
                    "title": "Helmut Lotti Goes Classic",
                    "date": date,
                    "url": _ticket_url(ticket.get("href") if ticket else None),
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": None,
                }
            )

        for row in table.select("tbody tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            date = _parse_date(cells[0].get_text(" ", strip=True))
            location = _location(_clean_text(cells[1].get_text(" ", strip=True)))
            title = _clean_text(cells[2].get_text(" ", strip=True))
            if not date or location is None or not title:
                log_message(
                    "Skipping incomplete tour row",
                    event="crawler_record_skipped",
                    url=SOURCE_URL,
                )
                continue

            venue, city, country_code = location
            ticket = cells[3].find("a", href=True)
            records.append(
                {
                    "title": title,
                    "date": date,
                    "url": _ticket_url(ticket.get("href") if ticket else None),
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": None,
                }
            )

        log_message(
            "Tour page parsed",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    HelmutLottiCrawler().run()


if __name__ == "__main__":
    main()
