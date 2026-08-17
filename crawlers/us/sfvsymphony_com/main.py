import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "San Fernando Valley Symphony Orchestra"
SOURCE_URL = "https://sfvsymphony.com/"
EVENTS_URL = "https://sfvsymphony.com/home"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
}

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
FULL_DATE_RE = re.compile(rf"\b({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})\b", re.I)
SHARED_MONTH_RE = re.compile(
    rf"\b({MONTHS})\s+(\d{{1,2}})\s*(?:&|and)\s*(\d{{1,2}}),\s*(\d{{4}})\b",
    re.I,
)
TIME_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?)\s*([ap])\.?m\.?\b", re.I)
CITY_RE = re.compile(r"^\s*([^,\n]+),\s*CA(?:\s+\d{5}(?:-\d{4})?)?\s*$", re.I)


def _clean(value):
    if not value:
        return None
    cleaned = " ".join(value.replace("\xa0", " ").split())
    return cleaned or None


def _parse_date(month, day, year):
    return datetime.strptime(f"{month} {day}, {year}", "%B %d, %Y").date().isoformat()


def _extract_dates(text):
    dates = []
    for month, first_day, second_day, year in SHARED_MONTH_RE.findall(text):
        dates.extend(
            [_parse_date(month, first_day, year), _parse_date(month, second_day, year)]
        )
    for month, day, year in FULL_DATE_RE.findall(text):
        dates.append(_parse_date(month, day, year))
    return list(dict.fromkeys(dates))


def _extract_time(text):
    matches = TIME_RE.findall(text)
    if not matches:
        return None
    value, meridiem = next(
        ((value, meridiem) for value, meridiem in matches if ":" in value),
        matches[0],
    )
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(f"{value}{meridiem.upper()}M", fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _event_details(container):
    lines = [
        cleaned
        for value in container.get_text("\n").splitlines()
        if (cleaned := _clean(value))
    ]
    venue = None
    city = None
    for index, line in enumerate(lines):
        if line.casefold().startswith("at ") and len(line) > 3:
            venue = _clean(line[3:])
            for candidate in lines[index + 1:index + 4]:
                match = CITY_RE.match(candidate)
                if match:
                    city = _clean(match.group(1))
                    break
            break
    return venue, city, "\n".join(lines)


class SfvSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sfvsymphony_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching concert page", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        seen_containers = set()
        for heading in soup.select("h2"):
            heading_text = _clean(heading.get_text(" "))
            if not heading_text or not _extract_dates(heading_text):
                continue

            container = heading.find_parent("div", class_=re.compile(r"zoogle-column"))
            if container is None or id(container) in seen_containers:
                continue
            seen_containers.add(id(container))

            venue, city, description = _event_details(container)
            dates = _extract_dates(description)
            if not dates or not venue or not city:
                log_message(
                    "Skipping event without a complete date and location",
                    event="crawler_record_skipped",
                    url=EVENTS_URL,
                    error_type="IncompleteEventLocation",
                    error_message="Required date, venue, or city was not found",
                )
                continue

            title = heading_text
            if " - " in title:
                title = _clean(title.rsplit(" - ", 1)[1])
            time_from = _extract_time(description)

            for event_date in dates:
                records.append(
                    {
                        "title": title,
                        "date": event_date,
                        "url": EVENTS_URL,
                        "time_from": time_from,
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "description": description,
                    }
                )

        log_message(
            "Concert page parsed",
            event="crawler_parse_completed",
            url=EVENTS_URL,
            record_count=len(records),
        )
        return records


def main():
    SfvSymphonyCrawler().run()


if __name__ == "__main__":
    main()
