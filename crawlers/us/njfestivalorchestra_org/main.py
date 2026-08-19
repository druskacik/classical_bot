import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "New Jersey Festival Orchestra"
SOURCE_URL = "https://www.njfestivalorchestra.org/"
LISTING_URLS = [
    f"{SOURCE_URL}concerts",
    f"{SOURCE_URL}season-archive",
]

HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
}

DATE_RE = re.compile(
    r"(?:MON(?:DAY)?|TUE(?:S|SDAY)?|WED(?:NESDAY)?|THU(?:R|RS|RSDAY)?|"
    r"FRI(?:DAY)?|SAT(?:URDAY)?|SUN(?:DAY)?)?,?\s*"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),?\s+(20\d{2})"
    r"(?:\s+(?:at\s*)?(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)))?",
    re.IGNORECASE,
)

CITY_PATTERNS = {
    "Westfield": re.compile(r"Westfield\s*,\s*NJ", re.IGNORECASE),
    "Basking Ridge": re.compile(r"Basking Ridge\s*,?\s*NJ", re.IGNORECASE),
    "Madison": re.compile(r"Madison\s*,\s*NJ", re.IGNORECASE),
    "Springfield": re.compile(r"Springfield\s*,\s*NJ", re.IGNORECASE),
    "Hoboken": re.compile(r"Hoboken\s*,\s*NJ", re.IGNORECASE),
    "Fort Lee": re.compile(r"Fort Lee\s*,\s*NJ", re.IGNORECASE),
    "Monroe Township": re.compile(r"Monroe Township\s*,\s*NJ", re.IGNORECASE),
    "Scotch Plains": re.compile(r"Scotch Plains\s*,\s*NJ", re.IGNORECASE),
}

VENUES = [
    "The Presbyterian Church | Chapel",
    "The Presbyterian Church",
    "Presbyterian Church",
    "Sieminski Theater",
    "Sieminski Theatre",
    "Westfield High School Auditorium",
    "Westfield High School",
    "Drew University",
    "The Concert Hall, Drew University",
    "The Concert Hall",
    "The James Ward Mansion",
    "Edison Intermediate School Auditorium",
    "St. Helen's Church",
    "Saint Helen's Church",
    "Renaissance Church",
    "First United Methodist Church",
    "Hertell Gardens",
    "The Hertell Gardens",
    "Rear gardens at 241 E. Dudley Avenue",
    "Stevens Institute of Technology",
    "Barrymore Film Center",
    "Monroe High School Performing Arts Center",
    "Fellowship Cultural Arts Center",
    "Shackamaxon Country Club",
]


def clean_text(value):
    value = value.replace("\u200b", " ").replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def parse_time(value):
    if not value:
        return None
    normalized = value.lower().replace(".", "").replace(" ", "")
    for pattern in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(normalized, pattern).strftime("%H:%M")
        except ValueError:
            pass
    return None


def event_title(section):
    candidates = []
    for element in section.select(".wixui-rich-text"):
        markup = str(element)
        if "#B20000" not in markup and not re.search(r"font-size:(?:3[0-9]|4[0-9])px", markup):
            continue
        text = clean_text(element.get_text(" ", strip=True))
        if not text or len(text) > 180:
            continue
        if text.casefold().startswith(("artists", "program", "guest artist")):
            continue
        if "season finale has moved" in text.casefold():
            continue
        candidates.append(text)
    return candidates[0] if candidates else None


def nearest_location(text, date_position, occurrence_index, occurrence_count):
    cities = []
    for city, pattern in CITY_PATTERNS.items():
        cities.extend(
            (abs(match.start() - date_position), match.start(), city)
            for match in pattern.finditer(text)
        )
    if not cities:
        return None, None
    cities.sort(key=lambda item: item[1])
    if len(cities) >= occurrence_count:
        _, city_position, city = cities[occurrence_index]
    else:
        _, city_position, city = min(cities)

    venues = []
    for venue in VENUES:
        venue_pattern = re.escape(venue).replace(r"\ ", r"\s+")
        for match in re.finditer(venue_pattern, text, re.IGNORECASE):
            # Venue and city normally appear together; the distance cap avoids
            # borrowing a hall from another occurrence in the same programme.
            distance = abs(match.start() - city_position)
            if distance <= 180:
                venues.append((distance, match.start(), venue))
    if not venues:
        return None, None
    _, _, venue = min(venues)
    return venue, city


def parse_section(section, page_url):
    title = event_title(section)
    description = clean_text(section.get_text("\n", strip=True))
    if not title or not re.search(r"\b(?:New Jersey|NJ) Festival Orchestra\b", description):
        return []
    if title.casefold().startswith("a toast to njfo"):
        return []

    records = []
    date_matches = list(DATE_RE.finditer(description))
    for occurrence_index, match in enumerate(date_matches):
        try:
            date = datetime.strptime(
                f"{match.group(1)} {match.group(2)} {match.group(3)}", "%B %d %Y"
            ).date().isoformat()
        except ValueError:
            continue

        venue, city = nearest_location(
            description, match.start(), occurrence_index, len(date_matches)
        )
        if not venue or not city:
            continue

        times = [parse_time(match.group(4))]
        if not times[0]:
            prefix = description[max(0, match.start() - 80):match.start()]
            found = re.findall(r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b", prefix, re.I)
            times = [parse_time(value) for value in found[-2:]] or [None]

        for time_from in dict.fromkeys(times):
            records.append(
                {
                    "title": title,
                    "date": date,
                    "url": page_url,
                    "time_from": time_from,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": "US",
                    "description": description,
                    "source_url": SOURCE_URL,
                    "source": SOURCE,
                }
            )
    return records


class NjFestivalOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="njfestivalorchestra_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def scrape(self):
        records = []
        session = requests.Session()
        for page_url in LISTING_URLS:
            log_message("Fetching concert page", event="crawler_url_fetch", url=page_url)
            response = session.get(page_url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for section in soup.select("main section"):
                records.extend(parse_section(section, page_url))

        unique = {}
        for record in records:
            # Wix occasionally nests the following visual section inside the
            # preceding section's SSR markup. Prefer the smaller, specific
            # section when both produce the same occurrence.
            key = (record["date"], record["time_from"], record["venue"])
            previous = unique.get(key)
            if previous is None or len(record["description"]) < len(previous["description"]):
                unique[key] = record
        result = sorted(
            unique.values(),
            key=lambda item: (item["date"], item["time_from"] or "", item["title"]),
        )
        log_message(
            "Concert pages parsed",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(result),
        )
        return result


def main():
    NjFestivalOrchestraOrgCrawler().run()


if __name__ == "__main__":
    main()
