import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.metorchestramusicians.org/"
SOURCE = "MET Orchestra Musicians"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/posts"

# The site is a news site, not a maintained event calendar.  Only create an
# occurrence when a post supplies both a performance time and an identifiable
# venue.  These are the venues used by concrete concert announcements in the
# site's archive; the order is significant because Weill Hall is within
# Carnegie Hall.
VENUES = (
    (re.compile(r"\bMorton H\. Meyerson Symphony Center\b", re.I),
     "Morton H. Meyerson Symphony Center", "Dallas"),
    (re.compile(r"\b(?:Weill (?:Recital )?Hall|Carnegie Hall['’]s Weill Hall)\b", re.I),
     "Weill Recital Hall at Carnegie Hall", "New York"),
    (re.compile(r"\bCarnegie Hall\b", re.I), "Carnegie Hall", "New York"),
    (re.compile(r"\b(?:Metropolitan Opera House|the Met Opera House)\b", re.I),
     "Metropolitan Opera House", "New York"),
    (re.compile(r"\bSymphony Space\b", re.I), "Symphony Space", "New York"),
    (re.compile(r"\bBrooklyn Public Library\b", re.I), "Brooklyn Public Library", "Brooklyn"),
    (re.compile(r"\bLincoln Center\b", re.I), "Lincoln Center", "New York"),
    (re.compile(r"\bHigh Line Nine Art Gallery\b", re.I),
     "High Line Nine Art Gallery", "New York"),
    (re.compile(r"\bReed Yeboah Fine Violins\b", re.I), "Reed Yeboah Fine Violins", "New York"),
)

MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"),
        start=1,
    )
}
DATE_TIME_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(20\d{2}))?"
    r"\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b",
    re.I,
)


def _plain_text(markup: str) -> str:
    soup = BeautifulSoup(html.unescape(markup), "html.parser")
    text = soup.get_text("\n", strip=True)
    return re.sub(r"[ \t]+", " ", text)


def _infer_year(month: int, day: int, published: datetime) -> int | None:
    """Resolve announcements such as a December post advertising January."""
    candidates = []
    for year in (published.year - 1, published.year, published.year + 1):
        try:
            candidate = datetime(year, month, day)
        except ValueError:
            continue
        delta = (candidate.date() - published.date()).days
        if -14 <= delta <= 400:
            candidates.append((abs(delta), year))
    return min(candidates)[1] if candidates else None


def _venue_for(text: str, anchor: int | None = None) -> tuple[str, str] | None:
    matches = []
    for priority, (pattern, venue, city) in enumerate(VENUES):
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 100):match.start()]
            if re.search(r"\b(?:since|after)\b.{0,60}\blast performance\b", prefix, re.I | re.S):
                continue
            distance = 0 if anchor is None else abs(match.start() - anchor)
            matches.append((distance, match.start(), priority, venue, city))
    if not matches:
        return None
    _, _, _, venue, city = min(matches)
    return venue, city


def _venue_near_occurrence(text: str, position: int) -> tuple[str, str] | None:
    nearby = text[max(0, position - 500):position + 500]
    venue = _venue_for(nearby, min(500, position))
    if venue is not None:
        return venue

    # Season announcements state the shared venue once before listing several
    # programmes.  Permit that explicit construction, but not incidental venue
    # mentions elsewhere in a long news article.
    introduction = text[:500]
    if re.search(r"\b(?:concerts?|concert series)\b.{0,100}\b(?:at|in)\b", introduction, re.I | re.S):
        return _venue_for(introduction)
    return None


def _post_records(post: dict) -> list[dict]:
    description = _plain_text(post.get("content", {}).get("rendered", ""))
    if not description:
        return []

    published = datetime.fromisoformat(post["date"])
    title = _plain_text(post.get("title", {}).get("rendered", ""))
    title_venue = _venue_for(title)
    records = []
    for match in DATE_TIME_RE.finditer(description):
        venue_info = title_venue or _venue_near_occurrence(description, match.start())
        if venue_info is None:
            continue
        venue, city = venue_info
        month = MONTHS[match.group(1).lower()]
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else _infer_year(month, day, published)
        if year is None:
            continue
        try:
            event_date = datetime(year, month, day).date().isoformat()
        except ValueError:
            continue

        hour = int(match.group(4)) % 12
        if match.group(6).lower() == "p":
            hour += 12
        minute = int(match.group(5) or 0)
        records.append({
            "title": title,
            "date": event_date,
            "url": post["link"],
            "time_from": f"{hour:02d}:{minute:02d}",
            "time_to": None,
            "venue": venue,
            "city": city,
            "description": description,
        })
    return records


class MetOrchestraMusiciansCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="metorchestramusicians_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self) -> list[dict]:
        records = []
        page = 1
        total_pages = 1
        with requests.Session() as session:
            session.headers["User-Agent"] = "classical-concert-crawler/1.0"
            while page <= total_pages:
                log_message("Fetching WordPress posts", event="crawler_url_fetch", url=API_URL, page=page)
                response = session.get(
                    API_URL,
                    params={
                        "per_page": 100,
                        "page": page,
                        "_fields": "date,link,title,content",
                    },
                    timeout=30,
                )
                response.raise_for_status()
                total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
                for post in response.json():
                    records.extend(_post_records(post))
                page += 1

        log_message("Parsed concert candidates", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    MetOrchestraMusiciansCrawler().run()


if __name__ == "__main__":
    main()
