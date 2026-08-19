import html
import re
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Spokane String Quartet"
SOURCE_URL = "https://www.spokanestringquartet.org/"
API_URL = f"{SOURCE_URL}wp-json/wp/v2"
DEFAULT_CITY = "Spokane"

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8,
    "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
DATE_RE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\.?\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", re.IGNORECASE)
VENUES = (
    (re.compile(r"\b(?:Martin Woldson Theater at )?[Tt]he Fox(?: Theater)?\b"), "The Fox Theater"),
    (re.compile(r"\b(?:[Tt]he )?Bing Crosby Theater\b"), "Bing Crosby Theater"),
    (re.compile(r"\bSpokane Falls Community College Music Building\b"), "Spokane Falls Community College Music Building"),
    (re.compile(r"\bCathedral of St\. John the Evangelist\b"), "Cathedral of St. John the Evangelist"),
)


def _clean(value):
    return " ".join(html.unescape(value or "").split()) or None


def _parse_time(value):
    match = TIME_RE.search(value or "")
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "p":
        hour += 12
    return f"{hour:02d}:{int(match.group(2) or 0):02d}:00"


def _event_date(match, published=None):
    month = MONTHS[match.group(1).lower().rstrip(".")]
    day = int(match.group(2))
    if match.group(3):
        years = [int(match.group(3))]
    elif published:
        # Publicity is normally posted shortly before a concert. Allow the
        # January-May portion of a season to fall in the following year.
        years = [published.year - 1, published.year, published.year + 1]
        years.sort(key=lambda year: abs((date(year, month, day) - published).days))
    else:
        return None
    for year in years:
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if match.group(3) or published - candidate <= timedelta(days=45):
            return candidate.isoformat()
    return None


def _venue(text):
    for pattern, canonical in VENUES:
        if pattern.search(text):
            return canonical
    return None


def _text_from_html(rendered):
    soup = BeautifulSoup(rendered or "", "html.parser")
    return _clean(soup.get_text("\n", strip=True))


class SpokaneStringQuartetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="spokanestringquartet_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["date", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def _get(self, path, **params):
        url = f"{API_URL}/{path}"
        log_message("Fetching WordPress API", event="crawler_url_fetch", url=url)
        response = requests.get(
            url,
            params=params,
            timeout=45,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
        )
        response.raise_for_status()
        return response.json()

    def _concert_page_records(self):
        pages = self._get("pages", slug="concerts", per_page=100)
        if not pages:
            return []
        soup = BeautifulSoup(pages[0]["content"]["rendered"], "html.parser")
        records = []
        for paragraph in soup.select("p"):
            lines = [_clean(line) for line in paragraph.get_text("\n").splitlines()]
            lines = [line for line in lines if line]
            if len(lines) < 2:
                continue
            date_match = DATE_RE.search(lines[0])
            venue = _venue(" ".join(lines[:3]))
            event_date = _event_date(date_match) if date_match else None
            if not event_date or not venue:
                continue
            description = "\n".join(lines[2:]) or None
            records.append({
                "title": f"Spokane String Quartet — {event_date}",
                "date": event_date,
                "url": pages[0]["link"],
                "time_from": _parse_time(lines[0]),
                "time_to": None,
                "venue": venue,
                "city": DEFAULT_CITY,
                "description": description,
            })
        return records

    def _archive_records(self):
        posts = self._get("posts", per_page=100, page=1)
        records = []
        for post in posts:
            title = _text_from_html(post["title"]["rendered"])
            body = _text_from_html(post["content"]["rendered"])
            if re.search(r"\b(?:season tickets?|tickets? (?:now )?on sale|concert dates?|dates announced|schedule)\b", title or "", re.IGNORECASE):
                continue
            lead = body[:900] if body else ""
            published = datetime.fromisoformat(post["date"]).date()
            date_match = DATE_RE.search(lead)
            venue = _venue(lead)
            event_date = _event_date(date_match, published) if date_match else None
            # These three concrete attributes distinguish performance notices
            # from ticket sales, season announcements, and organization news.
            if not title or not event_date or not venue or not _parse_time(lead):
                continue
            records.append({
                "title": title,
                "date": event_date,
                "url": post["link"],
                "time_from": _parse_time(lead),
                "time_to": None,
                "venue": venue,
                "city": DEFAULT_CITY,
                "description": body,
            })
        return records

    def scrape(self):
        # Archive posts come first so their detailed programme text wins the
        # date/venue deduplication over the compact current-season listing.
        records = self._archive_records() + self._concert_page_records()
        log_message(
            "Concert records parsed",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    SpokaneStringQuartetCrawler().run()


if __name__ == "__main__":
    main()
