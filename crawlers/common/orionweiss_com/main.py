import calendar
import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Orion Weiss"
SOURCE_URL = "https://www.orionweiss.com/"
SCHEDULE_URL = urljoin(SOURCE_URL, "schedule")
HEADERS = {"User-Agent": "classical-bot/1.0 (+https://www.orionweiss.com/)"}

# These are venue defaults only where the schedule identifies a venue, or where
# a presenter's calendar is tied to a well-established home venue in the named
# city. They are deliberately not general defaults for touring orchestras.
VENUES = {
    "Elgin Symphony Orchestra": "Hemmens Cultural Center",
    "Milwaukee Symphony Orchestra": "Bradley Symphony Center",
    "Pasadena Symphony Orchestra": "Ambassador Auditorium",
    "Arkansas Symphony Orchestra": "Clinton Presidential Center",
    "Monterey Symphony Orchestra": "Sunset Center",
    "Chamber Music Society of Fort Worth": "Modern Art Museum of Fort Worth",
    "Boston Symphony Orchestra": "Symphony Hall",
    "Chamber Music Society of Lincoln Center": "Alice Tully Hall",
    "Peoria Symphony Orchestra": "Peoria Civic Center Theater",
    "UKARIA": "UKARIA Cultural Centre",
    "Charlotte Symphony": "Belk Theater",
    "Santa Rosa Symphony": "Weill Hall at the Green Music Center",
    "Cheboygan Chamberfest": "Cheboygan Opera House",
    "Seattle Chamber Music": "Nordstrom Recital Hall",
    "Fort Worth Symphony Orchestra": "Bass Performance Hall",
    "Tulsa Symphony": "Tulsa Performing Arts Center",
    "Asheville Symphony": "Thomas Wolfe Auditorium",
}

COUNTRIES = {
    "Norway": "NO", "Hong Kong": "HK", "Japan": "JP",
    "South Korea": "KR", "Italy": "IT", "Taiwan": "TW",
    "Mexico": "MX", "South Australia": "AU", "Australia": "AU",
    "Canada": "CA",
}
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC", "D.C.",
}
CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}


def _location(text):
    match = re.search(r"\(([^()]*)\)\s*$", text)
    if not match:
        if "Wigmore Hall London" in text:
            return "London", "GB"
        return None, None
    value = match.group(1).strip()
    if value == "South Australia":
        return "Mount Barker", "AU"
    if "," in value:
        city, region = (part.strip() for part in value.rsplit(",", 1))
        region = region.upper()
        if region in US_STATES:
            return {"PeorIa": "Peoria"}.get(city, city), "US"
        if region in CANADIAN_PROVINCES:
            return city, "CA"
    for name, code in COUNTRIES.items():
        if value.casefold() == name.casefold():
            # City-states and schedule entries which use a city as the country label.
            return value, code
        if value.casefold().endswith(", " + name.casefold()):
            return value[: -(len(name) + 2)], code
    return None, None


def _venue(title, city):
    if "Chamber Music Society of Lincoln Center" in title:
        return "Alice Tully Hall" if city == "New York" else "Harris Theater" if city == "Chicago" else None
    for needle, venue in VENUES.items():
        if needle.casefold() in title.casefold():
            return venue
    explicit = re.findall(
        r"(?:^|\s•\s)([^•]*(?:Hall|Center|Centre|University|School|Foundation|"
        r"Kennedy Center|Royal Conservatory|Bender JCC|BIG Arts|Rancho La Puerta|"
        r"Music Mountain)[^•()]*)",
        title,
        flags=re.I,
    )
    return explicit[-1].strip(" -") if explicit else None


def _dates(value):
    value = re.sub(r"\s+", " ", value.strip()).replace(" & ", "-")
    full = re.fullmatch(r"([A-Za-z]+) (\d{1,2})(?:\s*-\s*(\d{1,2}))?, (\d{4})", value)
    if full:
        month, first, last, year = full.groups()
        start = date(int(year), list(calendar.month_name).index(month), int(first))
        end = start.replace(day=int(last or first))
    else:
        cross = re.fullmatch(
            r"([A-Za-z]+) (\d{1,2})\s*-\s*([A-Za-z]+) (\d{1,2}), (\d{4})", value
        )
        if not cross:
            return []
        m1, d1, m2, d2, year = cross.groups()
        start = date(int(year), list(calendar.month_name).index(m1), int(d1))
        end = date(int(year), list(calendar.month_name).index(m2), int(d2))
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def _parse_entry(text, url):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or "•" not in lines[0]:
        return []
    date_text, title = (part.strip() for part in lines[0].split("•", 1))
    dates = _dates(date_text)
    city, country = _location(title)
    venue = _venue(title, city)
    if not dates or not city or not country or not venue:
        return []
    title = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()
    description = "\n".join(lines[1:]) or None
    return [{
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": None,
        "venue": venue,
        "city": city,
        "country_code": country,
        "description": description,
    } for event_date in dates]


class OrionWeissCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="orionweiss_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching schedule", event="crawler_url_fetch", url=SCHEDULE_URL)
        response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []
        pending = None
        for block in soup.select("main .sqs-block"):
            if "sqs-block-html" in (block.get("class") or []):
                for paragraph in block.select(".sqs-html-content p"):
                    strong = paragraph.find("strong")
                    header = strong.get_text(" ", strip=True) if strong else ""
                    complete = paragraph.get_text(" ", strip=True)
                    description = complete[len(header):].strip() if complete.startswith(header) else ""
                    text = header + (("\n" + description) if description else "")
                    if re.match(r"^[A-Z][a-z]+ \d", text) and "•" in text:
                        pending = text
                        # Archive entries have no detail link and are complete here.
                        records.extend(_parse_entry(text, SCHEDULE_URL))
            elif pending and "sqs-block-button" in (block.get("class") or []):
                link = block.find("a", href=True)
                if link:
                    # Replace the just-added schedule-URL version with the ticket URL.
                    parsed = _parse_entry(pending, urljoin(SCHEDULE_URL, link["href"]))
                    if parsed:
                        keys = {(r["title"], r["date"]) for r in parsed}
                        records = [r for r in records if (r["title"], r["date"]) not in keys]
                        records.extend(parsed)
                pending = None
        log_message("Schedule parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    OrionWeissCrawler().run()


if __name__ == "__main__":
    main()
