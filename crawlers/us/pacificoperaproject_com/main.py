import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Pacific Opera Project"
SOURCE_URL = "https://www.pacificoperaproject.com/"
SITEMAP_URL = f"{SOURCE_URL}pages-sitemap.xml"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
}

# These pages can contain dates and addresses, but are not concrete performances.
NON_EVENT_PATHS = {
    "",
    "/2025-2026-season",
    "/2026-2027-season",
    "/book-online",
    "/calendar",
    "/education",
    "/education-faqs",
    "/events",
    "/fieldtrip",
    "/inschool",
    "/pop-in-program",
    "/popaganza",
    "/production-history",
    "/season-history",
    "/subscribe",
    "/touring",
    "/youthchorus",
}

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
WEEKDAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Tues|Wed|Thu|Thur|Fri|Sat|Sun"
DATE_LINE_RE = re.compile(
    rf"\b(?:(?:{WEEKDAYS}),?\s+)?(?P<month>{MONTHS})\.?\s+(?P<day>\d{{1,2}})"
    rf"(?:st|nd|rd|th)?,?\s+(?P<year>20\d{{2}})\s*(?:\||at)?\s*"
    rf"(?P<times>\d{{1,2}}(?::\d{{2}})?\s*(?:a\.?m\.?|p\.?m\.?)(?:\s*&\s*\d{{1,2}}(?::\d{{2}})?\s*(?:a\.?m\.?|p\.?m\.?))?)?",
    re.IGNORECASE,
)
CITY_RE = re.compile(
    r"^(?P<city>[A-Za-z][A-Za-z .'-]+?)(?:,?\s+(?:CA|California))?\s+\d{5}(?:-\d{4})?$",
    re.IGNORECASE,
)
STREET_RE = re.compile(r"^\d+\s+.+")
TIME_RE = re.compile(r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)", re.IGNORECASE)


def _clean(value):
    if not value:
        return None
    return " ".join(str(value).replace("\u200b", " ").replace("\xa0", " ").split()) or None


def _title(soup):
    meta = soup.select_one('meta[property="og:title"]')
    value = meta.get("content") if meta else (soup.title.string if soup.title else None)
    value = _clean(value)
    return value.split(" | ", 1)[0].strip() if value else None


def _time(value):
    normalized = re.sub(r"\.", "", value).replace(" ", "").upper()
    for form in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(normalized, form).strftime("%H:%M:%S")
        except ValueError:
            pass
    return None


def _venue_and_city(lines):
    for index, line in enumerate(lines):
        match = CITY_RE.fullmatch(line)
        if not match:
            continue
        city = _clean(match.group("city"))
        previous = index - 1
        if previous >= 0 and STREET_RE.match(lines[previous]):
            previous -= 1
        if previous < 0:
            continue
        venue = _clean(lines[previous])
        if venue and city and venue.casefold() != city.casefold():
            return venue, city
    return None, None


def _occurrences(lines):
    occurrences = []
    # Wix sometimes splits a visible date, separator, and meridiem across
    # adjacent text nodes. Joining the nodes restores the displayed line.
    text = " ".join(lines)
    for match in DATE_LINE_RE.finditer(text):
        if not match or not match.group("times"):
            continue
        try:
            date = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {match.group('year')}",
                "%B %d %Y" if len(match.group("month")) > 4 else "%b %d %Y",
            ).date().isoformat()
        except ValueError:
            continue
        for raw_time in TIME_RE.findall(match.group("times")):
            parsed_time = _time(raw_time)
            if parsed_time:
                occurrences.append((date, parsed_time))
    return occurrences


class PacificOperaProjectComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="pacificoperaproject_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "venue",
            "city",
            "country_code",
            "description",
        ],
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(SITEMAP_URL, timeout=45)
        response.raise_for_status()
        sitemap = BeautifulSoup(response.text, "xml")

        records = []
        for node in sitemap.select("loc"):
            url = _clean(node.get_text())
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.netloc not in {"pacificoperaproject.com", "www.pacificoperaproject.com"}:
                continue
            if parsed.path.rstrip("/") in NON_EVENT_PATHS:
                continue

            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch Pacific Opera Project page",
                    event="crawler_item_failed",
                    level="warning",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            soup = BeautifulSoup(detail_response.text, "html.parser")
            main = soup.select_one("main")
            if main is None:
                continue
            for element in main.select("script, style, noscript"):
                element.decompose()
            lines = [_clean(value) for value in main.stripped_strings]
            lines = [value for value in lines if value]
            title = _title(soup)
            venue, city = _venue_and_city(lines)
            occurrences = _occurrences(lines)
            if not title or not venue or not city or not occurrences:
                continue

            description = _clean(main.get_text("\n", strip=True))
            for date, time_from in occurrences:
                records.append(
                    {
                        "title": title,
                        "date": date,
                        "url": url,
                        "time_from": time_from,
                        "venue": venue,
                        "city": city,
                        "country_code": "US",
                        "description": description,
                    }
                )

        log_message(
            "Pacific Opera Project pages parsed",
            event="crawler_scrape_completed",
            url=SITEMAP_URL,
            record_count=len(records),
        )
        return sorted(records, key=lambda item: (item["date"], item["time_from"], item["title"]))


def main():
    PacificOperaProjectComCrawler().run()


if __name__ == "__main__":
    main()
