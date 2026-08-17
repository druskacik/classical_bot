import html
import re
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Plano Symphony Orchestra"
SOURCE_URL = "https://planosymphony.org/"
API_URL = "https://planosymphony.org/wp-json/wp/v2/pages"

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8,
    "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
DATE_RE = re.compile(
    r"(?i)(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)?,?\s*"
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<year>20\d{2}))?"
)
TIME_RE = re.compile(r"(?i)\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)\b")

# Event pages use free-form Elementor text rather than structured venue fields.
# These are the first-party venue names observed in the page catalogue.
VENUES = (
    ("Robinson Fine Arts Center", "Plano", "US"),
    ("St. Andrew Methodist Church", "Plano", "US"),
    ("Hasley Chapel, St. Andrew Methodist Church", "Plano", "US"),
    ("Christ United Methodist Church", "Plano", "US"),
    ("Courtyard Theater", "Plano", "US"),
    ("Courtyard Theatre", "Plano", "US"),
    ("Nack Theater", "Frisco", "US"),
    ("Frisco Discovery Center Theatre", "Frisco", "US"),
    ("Frisco Discovery Center", "Frisco", "US"),
    ("North Texas Performing Arts Plano", "Plano", "US"),
    ("Addison Conference and Theatre Centre", "Addison", "US"),
    ("Addison Theatre Centre", "Addison", "US"),
    ("McKinney Performing Arts Center", "McKinney", "US"),
    ("Eisemann Center", "Richardson", "US"),
    ("Meyerson Symphony Center", "Dallas", "US"),
    ("ArtCentre of Plano", "Plano", "US"),
    ("Oak Point Park", "Plano", "US"),
    ("National Conservatory of Music", "Mexico City", "MX"),
)

# These are collection, season, registration, or calendar pages which repeat
# individual occurrences. Their linked detail pages are fetched independently.
OVERVIEW_SLUGS = {
    "calendar", "concerts-and-events", "family-series", "school-program",
    "special-events", "subscriptions", "summer-programs", "family-series-2022",
    "2025-26-season", "2025-26-season-renewals", "2026-2027-season",
}


def _clean_text(markup):
    soup = BeautifulSoup(markup or "", "html.parser")
    return "\n".join(
        value for value in (" ".join(line.split()) for line in soup.get_text("\n").splitlines())
        if value
    )


def _event_year(match, published):
    if match.group("year"):
        return int(match.group("year"))
    month = MONTHS[match.group("month").lower().rstrip(".")]
    day = int(match.group("day"))
    possibilities = []
    for year in range(published.year - 1, published.year + 2):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if published - timedelta(days=60) <= candidate <= published + timedelta(days=430):
            possibilities.append(candidate)
    return min(possibilities, key=lambda value: abs(value - published)).year if possibilities else None


def _venue_near(text, date_start, boundary):
    # On multi-date pages the venue follows its own date. Restricting the
    # search to that occurrence prevents the preceding venue being reused.
    after = text[date_start:min(boundary, date_start + 420)]
    folded = after.casefold()
    found = []
    for venue, city, country_code in VENUES:
        position = folded.find(venue.casefold())
        if position >= 0:
            found.append((position, -len(venue), venue, city, country_code))
    if found:
        _, _, venue, city, country_code = min(found)
        return venue, city, country_code

    # A small number of prose-style tour announcements name the hall just
    # before the date instead of after it.
    before = text[max(0, date_start - 220):date_start].casefold()
    found = []
    for venue, city, country_code in VENUES:
        position = before.rfind(venue.casefold())
        if position >= 0:
            found.append((-position, -len(venue), venue, city, country_code))
    if found:
        _, _, venue, city, country_code = min(found)
        return venue, city, country_code
    return None


def _clock_value(match):
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    marker = match.group("ampm").lower().replace(".", "")
    if hour > 12 or minute > 59:
        return None
    if marker == "pm" and hour != 12:
        hour += 12
    elif marker == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}:00"


def _times(segment, matches):
    values = [(match, _clock_value(match)) for match in matches]
    values = [(match, value) for match, value in values if value]
    if not values:
        return []
    if len(values) >= 2:
        between = segment[values[0][0].end():values[1][0].start()]
        if re.search(r"[–—-]|\bto\b", between, re.I) and "&" not in between:
            return [(values[0][1], values[1][1])]

    # In forms such as "7–11 PM", the first time inherits the meridiem.
    first_match, first_value = values[0]
    prefix = segment[max(0, first_match.start() - 18):first_match.start()]
    inherited = re.search(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*[–—-]\s*$", prefix)
    if inherited:
        marker = first_match.group("ampm")
        synthetic = TIME_RE.search(f"{inherited.group('hour')}:{inherited.group('minute') or '00'} {marker}")
        start_value = _clock_value(synthetic)
        return [(start_value, first_value)] if start_value else []
    return [(value, None) for _, value in values]


def _parse_page(page):
    slug = page.get("slug", "")
    if slug in OVERVIEW_SLUGS or re.fullmatch(r"20\d{2}(?:-20\d{2}|-\d{2})?-season", slug):
        return []

    title = html.unescape(BeautifulSoup(page.get("title", {}).get("rendered", ""), "html.parser").get_text(" "))
    title = " ".join(title.split())
    url = page.get("link")
    text = _clean_text(page.get("content", {}).get("rendered"))
    if not title or not url or not text:
        return []

    try:
        published = datetime.fromisoformat(page["date"]).date()
    except (KeyError, TypeError, ValueError):
        return []

    matches = list(DATE_RE.finditer(text))
    records = []
    for index, match in enumerate(matches):
        boundary = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        after_date = text[match.end():min(boundary, match.end() + 180)]
        time_matches = list(TIME_RE.finditer(after_date))
        if not time_matches:
            continue

        venue_data = _venue_near(text, match.start(), boundary)
        if not venue_data:
            continue
        venue, city, country_code = venue_data
        year = _event_year(match, published)
        if year is None:
            continue
        try:
            event_date = date(
                year,
                MONTHS[match.group("month").lower().rstrip(".")],
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            continue

        for time_from, time_to in _times(after_date, time_matches):
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": time_from,
                    "time_to": time_to,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": text,
                }
            )
    return records


class PlanoSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="planosymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        page_number = 1
        total_pages = 1
        while page_number <= total_pages:
            log_message("Fetching WordPress event candidates", event="crawler_url_fetch", url=API_URL, page=page_number)
            response = requests.get(
                API_URL,
                params={
                    "per_page": 100,
                    "page": page_number,
                    "orderby": "id",
                    "order": "asc",
                    "_fields": "date,link,slug,title,content",
                },
                headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
                timeout=45,
            )
            response.raise_for_status()
            total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
            for page in response.json():
                records.extend(_parse_page(page))
            page_number += 1

        log_message("WordPress event candidates parsed", event="crawler_scrape_completed", url=API_URL, record_count=len(records))
        return records


def main():
    PlanoSymphonyCrawler().run()


if __name__ == "__main__":
    main()
