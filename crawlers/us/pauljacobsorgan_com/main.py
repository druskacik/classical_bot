import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Paul Jacobs"
SOURCE_URL = "https://www.pauljacobsorgan.com/"
CONCERTS_URL = f"{SOURCE_URL}concerts"

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}
MONTH_PATTERN = "|".join(MONTHS)
DATE_LINE_RE = re.compile(
    rf"^(?P<month>{MONTH_PATTERN}) (?P<day>\d{{1,2}})"
    rf"(?:-(?:(?P<end_month>{MONTH_PATTERN}) )?(?P<end_day>\d{{1,2}}))?"
    rf"(?:, (?P<year>\d{{4}}))?$"
)
CITY_RE = re.compile(r"^(?P<city>.+?),\s*(?P<region>[A-Z]{2})(?:,\s*Canada)?$")
SEASON_RE = re.compile(r"^\d{4}-\d{2} Concerts$")
MONTH_YEAR_RE = re.compile(rf"^(?:{MONTH_PATTERN}) (?P<year>\d{{4}})$")
NON_VENUE_RE = re.compile(r"\b(?:festival|orchestra|symphony|pro musica|concerts?|recital)\b", re.I)


def _date_range(match: re.Match, fallback_year: int | None) -> list[date]:
    year = int(match.group("year")) if match.group("year") else fallback_year
    if year is None:
        return []

    start = date(year, MONTHS[match.group("month")], int(match.group("day")))
    if not match.group("end_day"):
        return [start]

    end_month = MONTHS[match.group("end_month") or match.group("month")]
    end_year = year + (end_month < start.month)
    end = date(end_year, end_month, int(match.group("end_day")))
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _location(line: str) -> tuple[str, str] | None:
    if line.endswith(", Switzerland"):
        return line.rsplit(",", 1)[0].strip(), "CH"
    match = CITY_RE.match(line)
    if not match:
        return None
    country_code = "CA" if line.endswith(", Canada") else "US"
    return match.group("city").strip(), country_code


def _event_chunks(lines: list[str]):
    current: list[str] = []
    fallback_year: int | None = None
    for line in lines:
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if SEASON_RE.match(line):
            if current:
                yield current, fallback_year
                current = []
            continue
        month_year = MONTH_YEAR_RE.match(line)
        if month_year:
            if current:
                yield current, fallback_year
                current = []
            fallback_year = int(month_year.group("year"))
            continue
        match = DATE_LINE_RE.match(line)
        if match:
            if current:
                yield current, fallback_year
            current = [line]
            if match.group("year"):
                fallback_year = int(match.group("year"))
        elif current:
            current.append(line)
    if current:
        yield current, fallback_year


def parse_concerts(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for element in soup.select("main div.wixui-rich-text"):
        lines = element.get_text("\n", strip=True).splitlines()
        if sum(bool(DATE_LINE_RE.match(line.strip())) for line in lines) >= 3:
            candidates.append(element)
    if not candidates:
        raise ValueError("Concert listing content was not found")

    listing = max(candidates, key=lambda element: len(element.get_text()))
    links = {
        anchor.get_text(" ", strip=True): anchor.get("href")
        for anchor in listing.select("a[href]")
        if anchor.get_text(" ", strip=True)
    }
    lines = listing.get_text("\n", strip=True).splitlines()
    records: list[dict] = []

    for chunk, fallback_year in _event_chunks(lines):
        match = DATE_LINE_RE.match(chunk[0])
        dates = _date_range(match, fallback_year) if match else []
        location_index = next(
            (index for index in range(len(chunk) - 1, 0, -1) if _location(chunk[index])),
            None,
        )
        if not dates or location_index is None or location_index < 2:
            continue

        city, country_code = _location(chunk[location_index])
        details = chunk[1:location_index]
        venue_lines = [line for line in details if not (line.startswith("(") and line.endswith(")"))]
        venue = venue_lines[-1].rstrip(",").strip() if venue_lines else ""
        if not venue:
            continue
        title_line = details[0]
        if len(venue_lines) == 1 and NON_VENUE_RE.search(venue):
            continue
        title = f"Paul Jacobs at {title_line}"
        event_url = next((links[line] for line in details if line in links), CONCERTS_URL)
        description = "\n".join(details)

        for event_date in dates:
            records.append(
                {
                    "title": title,
                    "date": event_date.isoformat(),
                    "url": event_url,
                    "time_from": None,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                }
            )
    return records


class PaulJacobsOrganCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="pauljacobsorgan_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=CONCERTS_URL)
        try:
            response = requests.get(
                CONCERTS_URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                "Concert calendar fetch failed",
                event="crawler_url_fetch_failed",
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return parse_concerts(response.text)


def main():
    PaulJacobsOrganCrawler().run()


if __name__ == "__main__":
    main()
