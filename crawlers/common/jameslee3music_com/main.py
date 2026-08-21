import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "James Lee III"
SOURCE_URL = "https://www.jameslee3music.com/"
CALENDAR_URL = f"{SOURCE_URL}calendar"

MONTHS = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December"
)
DATE_TOKEN_RE = re.compile(
    rf"(?P<month>{MONTHS})\s+(?P<days>\d{{1,2}}(?:\s*(?:[-–]|and)\s*\d{{1,2}})?)"
    rf"[,.]?\s+(?P<year>20\d{{2}})",
    re.IGNORECASE,
)
FUTURE_DATE_RE = re.compile(
    rf"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
    rf"(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}}),\s+(?P<year>20\d{{2}}),\s*"
    rf"(?P<time>\d{{1,2}}:\d{{2}}\s*[AP]M)",
    re.IGNORECASE,
)

# Older entries often give a hall and state, but omit the city. These are stable,
# venue-specific locations and let us retain otherwise well-formed archive rows.
ARCHIVE_VENUES = {
    "laeiszhalle hamburg": ("Laeiszhalle Hamburg", "Hamburg", "DE"),
    "heinz hall": ("Heinz Hall", "Pittsburgh", "US"),
    "thalia mara mall": ("Thalia Mara Hall", "Jackson", "US"),
    "the orpheum theater": ("The Orpheum Theater", "New Orleans", "US"),
    "crenshaw high school performing arts center": (
        "Crenshaw High School Performing Arts Center",
        "Los Angeles",
        "US",
    ),
    "steinmetz hall": ("Steinmetz Hall", "Orlando", "US"),
    "theater of the orlando lutheran towers": (
        "Theater of the Orlando Lutheran Towers",
        "Orlando",
        "US",
    ),
    "perelman theater": ("Perelman Theater", "Philadelphia", "US"),
    "gates hall": ("Gates Hall", "Denver", "US"),
    "weill recital hall": ("Weill Recital Hall", "New York", "US"),
    "milton court concert hall": ("Milton Court Concert Hall", "London", "GB"),
    "strathmore music center": ("Strathmore Music Center", "North Bethesda", "US"),
}


def _iso_date(month: str, day: int, year: int) -> str:
    return datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date().isoformat()


def _dates(match: re.Match) -> list[str]:
    month = match.group("month").title()
    year = int(match.group("year"))
    day_numbers = [int(value) for value in re.findall(r"\d+", match.group("days"))]
    if "and" in match.group("days").lower():
        days = day_numbers
    elif len(day_numbers) == 2:
        days = range(day_numbers[0], day_numbers[1] + 1)
    else:
        days = day_numbers
    return [_iso_date(month, day, year) for day in days]


def _archive_location(text: str):
    normalized = text.lower()
    for marker, location in ARCHIVE_VENUES.items():
        if marker in normalized:
            return location
    return None


def _future_records(soup: BeautifulSoup) -> list[dict]:
    records = []
    links = [
        node.get("href") or CALENDAR_URL
        for node in soup.select("main a")
        if node.get_text(" ", strip=True).lower() == "learn more"
    ]
    event_nodes = []
    for node in soup.select("main .sqs-html-content"):
        text = re.sub(r"[ \t]+", " ", node.get_text("\n", strip=True))
        if FUTURE_DATE_RE.search(text):
            event_nodes.append((node, text))

    for index, (_, text) in enumerate(event_nodes):
        match = FUTURE_DATE_RE.search(text)
        assert match is not None

        before = text[: match.start()].strip(" \n")
        after = [line.strip() for line in text[match.end() :].splitlines() if line.strip()]
        if len(after) < 2:
            continue
        venue, city_region = after[0], after[1]
        city_match = re.match(r"(.+?),\s*[A-Z]{2}$", city_region)
        if not before or not venue or not city_match:
            continue

        records.append(
            {
                "title": re.sub(r"\s+", " ", before),
                "date": _iso_date(match.group("month").title(), int(match.group("day")), int(match.group("year"))),
                "url": links[index] if index < len(links) else CALENDAR_URL,
                "time_from": datetime.strptime(match.group("time").upper(), "%I:%M %p").time().isoformat(),
                "venue": venue,
                "city": city_match.group(1).strip(),
                "country_code": "US",
                "description": re.sub(r"\s+", " ", text),
            }
        )
    return records


def _archive_records(soup: BeautifulSoup) -> list[dict]:
    heading = next(
        (tag for tag in soup.select("main h1") if "past performances" in tag.get_text(" ", strip=True).lower()),
        None,
    )
    if heading is None:
        return []
    container = heading.find_parent(class_="sqs-html-content")
    if container is None:
        return []

    text = container.get_text("\n", strip=True)
    matches = list(DATE_TOKEN_RE.finditer(text))
    records = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        entry = re.sub(r"\s+", " ", text[match.end() : end]).strip(" :;,.\n")
        location = _archive_location(entry)
        if not entry or location is None:
            continue
        venue, city, country_code = location
        title = entry.split("|")[0].strip(" ”\"")
        for event_date in _dates(match):
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": CALENDAR_URL,
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": entry,
                }
            )
    return records


class JamesLee3MusicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jameslee3music_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = _future_records(soup) + _archive_records(soup)
        log_message(
            "Calendar parsed",
            event="crawler_scrape_completed",
            url=CALENDAR_URL,
            record_count=len(records),
        )
        return records


def main():
    JamesLee3MusicCrawler().run()


if __name__ == "__main__":
    main()
