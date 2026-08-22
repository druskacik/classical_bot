import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://sevangharibian.net/"
SOURCE = "Sevan Gharibian"

COUNTRY_CODES = {
    "Armenia": "AM",
    "Germany": "DE",
    "Italy": "IT",
    "Spain": "ES",
    "United States": "US",
}


def _clean_text(element) -> str:
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def _parse_time(text: str) -> str | None:
    matches = re.findall(r"(\d{1,2}(?:[.:]\d{2})?)\s*([ap])\.m\.", text, re.IGNORECASE)
    if not matches:
        return None

    # When a pre-concert talk is listed, the performance is the final time.
    # For ordinary "from ... to ..." ranges, the first time is the start.
    value, meridiem = matches[-1] if "pre-concert" in text.lower() else matches[0]
    value = value.replace(".", ":")
    if ":" not in value:
        value += ":00"
    parsed = datetime.strptime(f"{value} {meridiem.lower()}m", "%I:%M %p")
    return parsed.strftime("%H:%M")


def _parse_end_time(text: str) -> str | None:
    if " to " not in text.lower():
        return None
    matches = re.findall(r"(\d{1,2}(?:[.:]\d{2})?)\s*([ap])\.m\.", text, re.IGNORECASE)
    if len(matches) < 2:
        return None
    value, meridiem = matches[1]
    value = value.replace(".", ":")
    if ":" not in value:
        value += ":00"
    parsed = datetime.strptime(f"{value} {meridiem.lower()}m", "%I:%M %p")
    return parsed.strftime("%H:%M")


def _parse_location(text: str) -> tuple[str, str, str] | None:
    location = text.removeprefix("📍").strip()
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(parts) < 3:
        return None

    country_name = parts[-1]
    country_code = COUNTRY_CODES.get(country_name)
    if not country_code:
        return None

    if country_code == "US" and len(parts) >= 4:
        venue = ", ".join(parts[:-3]).strip()
        city = parts[-3]
    else:
        venue = ", ".join(parts[:-2]).strip()
        city = parts[-2]
    if city == "Florece":
        city = "Florence"
    if not venue or not city:
        return None
    return venue, city, country_code


class SevanGharibianCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sevangharibian_net",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching calendar", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(
            SOURCE_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        calendar = soup.select_one("#calendar-section")
        if calendar is None:
            raise ValueError("Calendar section was not found")

        records = []
        year = None
        for element in calendar.find_all(recursive=False):
            if element.name == "p" and re.fullmatch(r"20\d{2}", _clean_text(element)):
                year = int(_clean_text(element))
                continue
            if year is None or "container-component" not in element.get("class", []):
                continue

            inner = element.select_one(".inner")
            columns = inner.find_all("div", recursive=False) if inner else []
            if len(columns) < 2:
                continue
            left_paragraphs = columns[0].find_all("p")
            right_paragraphs = columns[1].find_all("p")
            if not left_paragraphs or not right_paragraphs:
                continue

            date_text = _clean_text(left_paragraphs[0])
            try:
                event_date = datetime.strptime(f"{date_text} {year}", "%d %B %Y").date().isoformat()
            except ValueError:
                continue

            title = _clean_text(right_paragraphs[0])
            location_paragraph = next(
                (p for p in right_paragraphs[1:] if _clean_text(p).startswith("📍")),
                None,
            )
            if not title or location_paragraph is None:
                continue
            location = _parse_location(_clean_text(location_paragraph))
            if location is None:
                continue
            venue, city, country_code = location

            info_link = next(
                (
                    link.get("href")
                    for link in right_paragraphs[1:]
                    for link in link.find_all("a", href=True)
                    if "more info" in _clean_text(link).lower()
                ),
                None,
            )
            event_url = urljoin(SOURCE_URL, info_link) if info_link else f"{SOURCE_URL}#calendar"
            time_text = " ".join(_clean_text(p) for p in left_paragraphs[1:])
            description_parts = [
                _clean_text(p)
                for p in right_paragraphs[1:]
                if p is not location_paragraph and not _clean_text(p).startswith("ℹ️")
            ]

            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": event_url,
                    "time_from": _parse_time(time_text),
                    "time_to": _parse_end_time(time_text),
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": "\n".join(description_parts) or None,
                }
            )

        log_message(
            "Calendar parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    SevanGharibianCrawler().run()


if __name__ == "__main__":
    main()
