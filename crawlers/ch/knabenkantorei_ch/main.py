import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://knabenkantorei.ch/"
SOURCE = "Knabenkantorei Basel"
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _split_venue_city(location: str) -> tuple[str | None, str | None]:
    """Split the compact location strings used by the agenda."""
    location = _clean_text(location)
    if not location:
        return None, None

    if "," in location:
        venue, city = (part.strip() for part in location.rsplit(",", 1))
        # A street without a building name is not a defensible venue.
        if re.search(r"(?:strasse|straße|gasse|weg|platz)\b", venue, re.I):
            return None, city or None
        return venue or None, city or None

    # The agenda commonly appends the municipality to a named institution.
    match = re.match(r"(.+?)\s+(Basel|Riehen|Allschwil|Muttenz)$", location, re.I)
    if match:
        return match.group(1).strip(), match.group(2).strip()

    return None, None


def _parse_agenda_date(raw: str) -> tuple[str | None, str | None, str | None, str]:
    parts = [_clean_text(part) for part in raw.replace("\xa0", " ").split("|")]
    if not parts:
        return None, None, None, ""

    dates = DATE_RE.findall(raw)
    if not dates:
        return None, None, None, ""
    try:
        event_date = datetime.strptime(dates[0], "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None, None, None, ""

    times = TIME_RE.findall(raw)
    time_from = times[0] if times else None
    time_to = times[1] if len(times) > 1 else None
    location = parts[-1] if len(parts) > 1 else ""
    return event_date, time_from, time_to, location


class KnabenkantoreiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="knabenkantorei_ch",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="CH",
        upload_target="potential",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching agenda", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for item in soup.select("#section-agenda .element-item[data-filter]"):
            title_node = item.select_one(".togglet h3")
            date_node = item.select_one(".togglet .date")
            if not title_node or not date_node:
                continue

            title = _clean_text(title_node.get_text(" ", strip=True))
            event_date, time_from, time_to, location = _parse_agenda_date(
                date_node.get_text(" ", strip=True)
            )
            venue, city = _split_venue_city(location)
            if not title or not event_date or not venue or not city:
                continue

            detail = item.select_one(".togglec")
            description = _clean_text(detail.get_text(" ", strip=True)) if detail else ""
            link = detail.select_one("a[href]") if detail else None
            url = urljoin(SOURCE_URL, link["href"]) if link else f"{SOURCE_URL}#agenda"

            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": time_from,
                    "time_to": time_to,
                    "venue": venue,
                    "city": city,
                    "description": description or None,
                }
            )

        log_message(
            "Agenda parsed",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    KnabenkantoreiCrawler().run()


if __name__ == "__main__":
    main()
