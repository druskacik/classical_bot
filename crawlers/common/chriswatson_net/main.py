import html
import re

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Chris Watson"
SOURCE_URL = "https://chriswatson.net/"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/posts"
PERFORMANCES_CATEGORY_ID = 6

MONTH = (
    r"January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
DATE_PATTERNS = (
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH})\s*,?\s+(20\d{{2}})\b", re.I),
    re.compile(rf"\b({MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,)?\s+(20\d{{2}})\b", re.I),
)
TIME_PATTERN = re.compile(r"\b(\d{1,2}(?:(?:[:.]\d{2}))?\s*(?:am|pm)|(?:[01]?\d|2[0-3])[:.]\d{2})\b", re.I)

# The archive is international.  This deliberately finite gazetteer is used only
# when a post explicitly names a place; unknown locations are skipped.
PLACES = {
    "Athens": "GR", "Belfast": "GB", "Berlin": "DE", "Bradford": "GB",
    "Brighton": "GB", "Cambridge": "GB", "Coimbra": "PT", "Edinburgh": "GB", "Gateshead": "GB",
    "Geneva": "CH", "Gladbeck": "DE", "Glasgow": "GB", "Guildford": "GB", "Harrogate": "GB",
    "Krakow": "PL", "Leeds": "GB", "Lincoln": "GB", "Liverpool": "GB",
    "London": "GB", "Malmesbury": "GB", "Monheim": "DE", "Newcastle": "GB",
    "Newlyn": "GB", "Paris": "FR", "Prague": "CZ", "Ravenglass": "GB",
    "Santa Cruz": "US", "Sheffield": "GB", "Snape": "GB", "Tasmania": "AU",
    "Troy": "US", "Warsaw": "PL", "Winnipeg": "CA", "York": "GB",
}
VENUE_WORDS = re.compile(
    r"\b(?:art centre|arts centre|beaconsfield|bluecoat|castle|cathedral|centquatre|"
    r"college|collection|empac|foundation|gallery|iklectik|indexical|king['’]s place|"
    r"lincoln cathedral|louvre|museum|national gallery|opera house|sage|snape maltings|"
    r"theatre|university|wallace collection)\b", re.I
)


def _text(value):
    return BeautifulSoup(html.unescape(value or ""), "html.parser").get_text(" ", strip=True)


def _content_lines(rendered):
    soup = BeautifulSoup(html.unescape(rendered or ""), "html.parser")
    return [re.sub(r"\s+", " ", value).strip() for value in soup.stripped_strings if value.strip()]


def _event_date(text):
    matches = []
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                parsed = date_parser.parse(match.group(0), fuzzy=True, dayfirst=True)
                matches.append((match.start(), parsed.date().isoformat()))
            except (ValueError, OverflowError):
                continue
    return min(matches)[1] if matches else None


def _location(title, lines):
    # Prefer the heading, then only the opening event-information lines.  Later
    # prose often names places from Watson's biography rather than this event.
    for haystack in (title, "\n".join(lines[:5])):
        for city, code in PLACES.items():
            if re.search(rf"\b{re.escape(city)}\b", haystack, re.I):
                return city, code
    # A country alone is not a city, so it only helps set a country after some
    # future city match; it is never returned as a placeholder city.
    return None


def _clean_venue(value, city):
    value = re.sub(rf"\b(?:{MONTH})\b.*$", "", value, flags=re.I)
    value = re.sub(r"\b(?:20\d{2}|\d{1,2}(?:st|nd|rd|th)?)\b.*$", "", value).strip(" ,|-/")
    def remove_city(match):
        prefix = value[:match.start()].rstrip().casefold()
        if match.start() == 0 or prefix.endswith(" of"):
            return match.group(0)
        return ""

    value = re.sub(rf"\b{re.escape(city)}\b", remove_city, value, flags=re.I).strip(" ,|-/")
    value = re.sub(r"\b(?:Australia|Austria|Canada|France|Germany|Greece|Ireland|Japan|Norway|Poland|Portugal|Switzerland|UK|USA)\b", "", value, flags=re.I)
    value = re.sub(r"(?:,?\s+on)$", "", value, flags=re.I)
    value = re.sub(r",\s*,", ",", value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" .,;:")
    if not value or value.casefold() == city.casefold() or len(value) > 100:
        return None
    return value


def _venue(title, lines, city):
    at_match = re.search(r"\s@\s([^|]+)", title)
    if at_match:
        venue = _clean_venue(at_match.group(1), city)
        if venue:
            return venue

    for line in lines[:5]:
        at_match = re.search(rf"\bat\s+(?:the\s+)?(.+?)(?=,|\s+\d{{1,2}}|\s+(?:{MONTH})\b|$)", line, re.I)
        if at_match and VENUE_WORDS.search(at_match.group(1)):
            venue = _clean_venue(at_match.group(1), city)
            if venue and len(venue.split()) <= 8:
                return venue

    candidates = re.split(r"\s*[|/]\s*", title) + lines[:12]
    for candidate in candidates:
        if VENUE_WORDS.search(candidate):
            relation_parts = re.split(r"\s+(?:at|in)\s+(?:the\s+)?", candidate, flags=re.I)
            matching_relation_parts = [part for part in relation_parts if VENUE_WORDS.search(part)]
            if len(matching_relation_parts) > 1:
                candidate = matching_relation_parts[-1]
            dash_parts = [part.strip() for part in re.split(r"\s+[–—-]\s+", candidate)]
            matching_dash_parts = [part for part in dash_parts if VENUE_WORDS.search(part)]
            if matching_dash_parts:
                candidate = matching_dash_parts[-1]
            named_parts = [part.strip() for part in candidate.split(",") if VENUE_WORDS.search(part)]
            if named_parts:
                candidate = min(named_parts, key=len)
            # Postal addresses follow the venue name on several posts.
            candidate = re.sub(r"\s+\d{1,4}\s+.*$", "", candidate)
            venue = _clean_venue(candidate, city)
            if venue:
                # Avoid treating a whole prose sentence as a venue.
                if (
                    len(venue.split()) <= 8
                    and not venue.endswith(".")
                    and not re.search(r"\b(?:will be|light|installation at)\b", venue, re.I)
                ):
                    return venue
    return None


def _time(lines):
    for line in lines[:12]:
        match = TIME_PATTERN.search(line)
        if match:
            try:
                return date_parser.parse(match.group(0).replace(".", ":")).strftime("%H:%M")
            except ValueError:
                pass
    return None


def _record(post):
    title = _text(post.get("title", {}).get("rendered"))
    lines = _content_lines(post.get("content", {}).get("rendered"))
    body = "\n".join(lines)
    combined = f"{title}\n{body}"
    date = _event_date(combined)
    location = _location(title, lines)
    if not date or not location:
        return None
    city, country_code = location
    venue = _venue(title, lines, city)
    if not venue:
        return None
    return {
        "title": title,
        "date": date,
        "url": post["link"],
        "time_from": _time(lines),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": body or None,
    }


class ChrisWatsonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="chriswatson_net",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self):
        records = []
        page = 1
        while True:
            log_message("Fetching performance archive", event="crawler_url_fetch", url=API_URL, page=page)
            response = requests.get(
                API_URL,
                params={
                    "categories": PERFORMANCES_CATEGORY_ID,
                    "per_page": 100,
                    "page": page,
                    "_fields": "id,link,title,content,categories",
                },
                timeout=30,
            )
            response.raise_for_status()
            posts = response.json()
            for post in posts:
                record = _record(post)
                if record:
                    records.append(record)
            total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
            if page >= total_pages:
                break
            page += 1
        log_message("Performance archive parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    ChrisWatsonCrawler().run()


if __name__ == "__main__":
    main()
