import re
from datetime import datetime
from html import unescape
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.qasim-naqvi.com/"
SOURCE = "Qasim Naqvi"
FEED_URL = f"{SOURCE_URL}concerts-and-events?format=json"
SITE_TIMEZONE = ZoneInfo("America/New_York")

# The collection's Squarespace location object contains only the site's default
# map pin.  These rules use venue and city names explicitly printed in each post.
VENUES = (
    (r"casa montju[iï]c", "Casa Montjuïc", "Barcelona", "ES"),
    (r"door open space", "Door Open Space", "Amsterdam", "NL"),
    (r"muziekgebouw", "Muziekgebouw", "Amsterdam", "NL"),
    (r"prix ars electronica|ars electronica", "Ars Electronica", "Linz", "AT"),
    (r"damrosch park", "Damrosch Park", "New York", "US"),
    (r"lincoln center", "Lincoln Center", "New York", "US"),
    (r"public records", "Public Records", "New York", "US"),
    (r"@lprnyc|\blpr\b|le poisson rouge", "Le Poisson Rouge", "New York", "US"),
    (r"@irl\.nyc|\birl\b", "IRL", "New York", "US"),
    (r"fridman gallery", "Fridman Gallery", "New York", "US"),
    (r"merkin hall", "Merkin Hall", "New York", "US"),
    (r"national sawdust", "National Sawdust", "New York", "US"),
    (r"first unitarian", "First Unitarian Congregational Society", "Brooklyn", "US"),
    (r"h0l0|holo", "H0L0", "New York", "US"),
    (r"southbank centre", "Southbank Centre", "London", "GB"),
    (r"tate modern", "Tate Modern", "London", "GB"),
    (r"spitalfields music festival", "Spitalfields Music Festival", "London", "GB"),
    (r"howard gilman opera house|\bbam(?:’s|'s|\b)", "BAM", "Brooklyn", "US"),
    (r"new jersey performing arts center|\bnjpac\b", "New Jersey Performing Arts Center", "Newark", "US"),
    (r"ultima festival", "Ultima Oslo Contemporary Music Festival", "Oslo", "NO"),
    (r"new music dublin", "New Music Dublin", "Dublin", "IE"),
    (r"musica nova helsinki", "Musica nova Helsinki", "Helsinki", "FI"),
    (r"north sea jazz festival", "North Sea Jazz Festival", "Rotterdam", "NL"),
    (r"novas frequencias festival", "Novas Frequências Festival", "Rio de Janeiro", "BR"),
    (r"the kitchen", "The Kitchen", "New York", "US"),
)

EVENT_RE = re.compile(
    r"\b(concert|perform(?:ance|ing|s|ed)?|playing|premiere|live at|live @|show)\b",
    re.IGNORECASE,
)
NON_EVENT_RE = re.compile(
    r"\b(album|ep|record) release\b|\breview(?:s|ed)?\b|award recipient|"
    r"featured (?:in|on)|music video|lessons|mentorship|grant\b|score (?:coming|panels)",
    re.IGNORECASE,
)


def _text(html: str | None) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()


def _location(title: str, description: str) -> tuple[str, str, str] | None:
    # Prefer a venue named in the title. Some posts advertise two separate
    # appearances in their body, and selecting the second one would fabricate
    # the venue for the titled event.
    if title.strip().casefold() == "solo at ooze":
        return None
    for text in (title, description):
        for pattern, venue, city, country_code in VENUES:
            if re.search(pattern, text, re.IGNORECASE):
                return venue, city, country_code
    return None


def _explicit_date(text: str, fallback: datetime) -> datetime:
    numeric = re.search(r"(?<!\d)(1[0-2]|0?[1-9])[/.-]([0-3]?\d)(?:[/.-](\d{2,4}))?(?!\d)", text)
    if numeric:
        month, day, year_text = numeric.groups()
        year = fallback.year if not year_text else int(year_text)
        if year < 100:
            year += 2000
        try:
            return fallback.replace(year=year, month=int(month), day=int(day))
        except ValueError:
            pass

    month_names = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    named = re.search(rf"\b({month_names})\.?\s+([0-3]?\d)(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?", text, re.IGNORECASE)
    if named:
        month_text, day, year_text = named.groups()
        month = datetime.strptime(month_text.rstrip(".")[:3].title(), "%b").month
        try:
            return fallback.replace(year=int(year_text or fallback.year), month=month, day=int(day))
        except ValueError:
            pass
    return fallback


def _date_is_defensible(text: str, event_date: datetime) -> bool:
    months = {
        name.lower(): number
        for number, names in enumerate(
            (("january", "jan"), ("february", "februrary", "feb"), ("march", "mar"), ("april", "apr"),
             ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
             ("september", "sept", "sep"), ("october", "oct"), ("november", "nov"),
             ("december", "dec")),
            1,
        )
        for name in names
    }
    mentioned = {number for name, number in months.items() if re.search(rf"\b{name}\b", text, re.IGNORECASE)}
    return not mentioned or event_date.month in mentioned


def _explicit_time(text: str) -> str | None:
    match = re.search(r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?(?!\w)", text, re.IGNORECASE)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == "p" else 0)
    return f"{hour:02d}:{int(minute or 0):02d}:00"


class QasimNaqviCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="qasim_naqvi_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        url = FEED_URL
        seen_pages = set()

        while url and url not in seen_pages:
            seen_pages.add(url)
            log_message("Fetching event feed", event="crawler_url_fetch", url=url)
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            payload = response.json()

            for item in [*(payload.get("upcoming") or []), *(payload.get("past") or [])]:
                title = unescape(_text(item.get("title")))
                description = unescape(_text(item.get("body")))
                evidence = f"{title} {description}"
                location = _location(title, description)
                start_ms = item.get("startDate")

                # This is a mixed news feed. Only concrete performance candidates
                # with an explicit recognizable venue and a real Squarespace date
                # are safe enough to send to the potential-event classifier.
                if (
                    not title
                    or not start_ms
                    or not location
                    or not EVENT_RE.search(evidence)
                    or NON_EVENT_RE.search(evidence)
                ):
                    continue

                start = datetime.fromtimestamp(start_ms / 1000, tz=SITE_TIMEZONE)
                event_date = _explicit_date(evidence, start)
                if not _date_is_defensible(title, event_date):
                    continue
                venue, city, country_code = location
                path = (item.get("fullUrl") or f"/concerts-and-events/{item['urlId']}").lstrip("/")
                records.append(
                    {
                        "title": title,
                        "date": event_date.date().isoformat(),
                        "url": f"{SOURCE_URL}{path}",
                        "time_from": _explicit_time(evidence),
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": description or None,
                    }
                )

            next_url = (payload.get("pagination") or {}).get("nextPageUrl")
            if next_url:
                separator = "&" if "?" in next_url else "?"
                url = f"{SOURCE_URL.rstrip('/')}{next_url}{separator}format=json"
            else:
                url = None

        log_message(
            "Scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    QasimNaqviCrawler().run()


if __name__ == "__main__":
    main()
