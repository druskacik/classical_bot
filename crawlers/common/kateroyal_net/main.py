import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Kate Royal"
SOURCE_URL = "https://www.kateroyal.net/"
SCHEDULE_URL = "https://www.kateroyal.net/copy-of-discography"

# The schedule is for a touring artist.  Only venues whose location is explicit
# and unambiguous are accepted; unknown tour stops are skipped rather than being
# assigned Kate Royal's home country or city.
VENUES = {
    "Cologne Philharmonie": ("Cologne", "DE"),
    "Royal Festival Hall": ("London", "GB"),
    "Royal Conservatoire of Scotland": ("Glasgow", "GB"),
}

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
TIME_RE = re.compile(r"^(\d{1,2})[.:](\d{2})(?:\s*([AP]M))?$", re.IGNORECASE)


def _parse_date(value):
    return datetime.strptime(value, "%m/%d/%Y").date().isoformat()


def _parse_time(value):
    match = TIME_RE.fullmatch(value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = (match.group(3) or "").upper()
    if minute > 59 or hour > 23:
        return None
    # The page currently publishes both a 24-hour value and a redundant "PM"
    # suffix (for example, "19.30 PM").
    if meridiem == "AM" and hour == 12:
        hour = 0
    elif meridiem == "PM" and hour < 12:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def _event_blocks(lines):
    starts = [index for index, line in enumerate(lines) if DATE_RE.fullmatch(line)]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        yield lines[start:end]


def _record_from_block(block):
    if len(block) < 3:
        return None

    try:
        event_date = _parse_date(block[0])
    except ValueError:
        return None

    time_from = _parse_time(block[1])
    detail_lines = block[2:]
    if not detail_lines:
        return None

    title = detail_lines[0]
    venue = next(
        (known_venue for known_venue in VENUES if any(known_venue in line for line in detail_lines)),
        None,
    )
    if not title or venue is None:
        return None

    city, country_code = VENUES[venue]
    return {
        "title": title,
        "date": event_date,
        "url": SCHEDULE_URL,
        "time_from": time_from,
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": "\n".join(detail_lines),
    }


class KateRoyalCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="kateroyal_net",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def scrape(self):
        log_message("Fetching schedule", event="crawler_url_fetch", url=SCHEDULE_URL)
        response = requests.get(SCHEDULE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        lines = [element.get_text(" ", strip=True) for element in soup.select("p")]
        lines = [line for line in lines if line and line != "\u200b"]
        if "Home" in lines:
            lines = lines[: lines.index("Home")]

        records = []
        for block in _event_blocks(lines):
            record = _record_from_block(block)
            if record is not None:
                records.append(record)
            else:
                log_message(
                    "Skipping schedule entry without a resolvable venue or date",
                    event="crawler_record_skipped",
                    url=SCHEDULE_URL,
                )
        return records


def main():
    KateRoyalCrawler().run()


if __name__ == "__main__":
    main()
