import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.brikcius.com/"
SOURCE = "František Brikcius"
FEED_URL = "https://www.brikcius.com/Brikcius.xml"

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}

VENUES = (
    ("Stone Bell House", "Stone Bell House"),
    ("Military Church of St. Jan Nepom", "Military Church of St. Jan Nepomucký"),
    ("Church of St. Salvator", "Church of St. Salvator"),
    ("Church of St. Jan Křtitel Na Prádle", "Church of St. Jan Křtitel Na Prádle"),
    ("Baroque Chapel of the Istituto Italiano di Cultura di Praga", "Baroque Chapel of the Istituto Italiano di Cultura di Praga"),
    ("Petrin Observation Tower", "Petřín Observation Tower"),
    ("Vaclav Havel Library", "Václav Havel Library"),
    ("Convent of St Agnes of Bohemia", "Convent of St Agnes of Bohemia"),
)

LOCATIONS = (
    (re.compile(r"\b(?:Prague|Praha)\b", re.I), "Prague", "CZ"),
    (re.compile(r"\bMoscow\b", re.I), "Moscow", "RU"),
    (re.compile(r"\bLondon\b", re.I), "London", "GB"),
)

DATE_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(20\d{2})\b",
    re.I,
)
TIME_RE = re.compile(
    r"\b(?:at|@)\s+(\d{1,2})(?:[.:](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b",
    re.I,
)


def _parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None
    try:
        return datetime(int(match.group(3)), month, int(match.group(1))).date().isoformat()
    except ValueError:
        return None


def _parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    period = match.group(3).lower().replace(".", "")
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if period == "pm" and hour != 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _venue(text):
    plain = text.translate(str.maketrans({"á": "a", "ř": "r", "ě": "e", "í": "i"}))
    for marker, venue in VENUES:
        if marker.lower() in plain.lower() or marker.lower() in text.lower():
            return venue
    return None


def _location(text):
    for pattern, city, country_code in LOCATIONS:
        if pattern.search(text):
            return city, country_code
    return None, None


def _clean_title(title):
    title = re.sub(r"^\s*\((?:INVITATION|PRESS)\)\s*", "", title, flags=re.I)
    title = re.sub(r"^\s*You are invited to (?:the )?", "", title, flags=re.I)
    title = re.sub(r"\s*-\s*https?://\S+\s*$", "", title, flags=re.I)
    return title.strip(" -")


def _is_candidate(title, description):
    text = f"{title} {description}".lower()
    concrete = DATE_RE.search(description) and any(
        phrase in text
        for phrase in ("concert", "recital", "cello suites", "cello sonatas", "organ")
    )
    # Festival ranges and recordings are overview/media records, not occurrences.
    excluded = any(
        phrase in text
        for phrase in ("film premiere", "trailer", "newsletter", "concert series")
    )
    return bool(concrete and not excluded)


class BrikciusCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="brikcius_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="CZ",
        upload_target="potential",
        dedupe_subset=["date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching Brikcius RSS feed", event="crawler_url_fetch", url=FEED_URL)
        response = requests.get(FEED_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "xml")
        records = []

        for item in soup.find_all("item"):
            title = item.title.get_text(" ", strip=True) if item.title else ""
            description = item.description.get_text("\n", strip=True) if item.description else ""
            if not title or not description or not _is_candidate(title, description):
                continue

            date = _parse_date(description)
            venue = _venue(description)
            city, country_code = _location(description)
            if not date or not venue or not city or not country_code:
                continue

            link = item.link.get_text(strip=True) if item.link else ""
            if not link and item.guid:
                link = item.guid.get_text(strip=True)
            if not link:
                link = FEED_URL

            records.append(
                {
                    "title": _clean_title(title),
                    "date": date,
                    "url": link,
                    "time_from": _parse_time(description),
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                }
            )

        log_message(
            "Parsed Brikcius RSS feed",
            event="crawler_scrape_completed",
            url=FEED_URL,
            record_count=len(records),
        )
        return records


def main():
    BrikciusCrawler().run()


if __name__ == "__main__":
    main()
