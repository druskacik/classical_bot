import html
import re
from collections import defaultdict, deque
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Bram van Sambeek"
SOURCE_URL = "http://bramvansambeek.com/"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/posts"
CALENDAR_URL = f"{SOURCE_URL}?feed=bvs-events-ical"
SCHEDULE_CATEGORY_ID = 2
TIMEOUT = 30

# The calendar has no LOCATION property. Only titles which explicitly name both
# a venue and its city are safe to emit. Keep longer/more specific names first.
VENUE_RULES = (
    (r"Muziekgebouw Frits Philips,? Eindhoven", "Muziekgebouw Frits Philips", "Eindhoven", "NL"),
    (r"Kleine zaal Concertgebouw,? Amsterdam", "Concertgebouw, Kleine Zaal", "Amsterdam", "NL"),
    (r"Elb Philharmonie Hamburg", "Elbphilharmonie", "Hamburg", "DE"),
    (r"Concertgebouw Amsterdam", "Concertgebouw", "Amsterdam", "NL"),
    (r"Concertgebouw,? Amsterdam", "Concertgebouw", "Amsterdam", "NL"),
    (r"Muziekgebouw Amsterdam", "Muziekgebouw aan 't IJ", "Amsterdam", "NL"),
    (r"Muziekgebouw,? Amsterdam", "Muziekgebouw aan 't IJ", "Amsterdam", "NL"),
    (r"Oosterpoort,? Groningen", "De Oosterpoort", "Groningen", "NL"),
    (r"[Dd]e [Dd]oelen,? [Rr]otterdam", "De Doelen", "Rotterdam", "NL"),
    (r"Toonzaal,? Den Bosch", "De Toonzaal", "'s-Hertogenbosch", "NL"),
    (r"Energiehuis,? Dordrecht", "Energiehuis", "Dordrecht", "NL"),
    (r"Noorderkerk,? Amsterdam", "Noorderkerk", "Amsterdam", "NL"),
    (r"Kasteel Duivenvoorde,? Voorschoten", "Kasteel Duivenvoorde", "Voorschoten", "NL"),
    (r"Berlin Konzerthaus", "Konzerthaus Berlin", "Berlin", "DE"),
    (r"Muziekcentrum,? Enschede", "Muziekcentrum Enschede", "Enschede", "NL"),
    (r"Breda Chass[eé]theater", "Chassé Theater", "Breda", "NL"),
    (r"Muzenforum,? Bloemendaal", "Muzenforum", "Bloemendaal", "NL"),
    (r"Nieuwe Veste,? Breda", "Nieuwe Veste", "Breda", "NL"),
    (r"Tivoli(?: Vredenburg)? Utrecht", "TivoliVredenburg", "Utrecht", "NL"),
    (r"Haarlem Philharmonie", "Philharmonie Haarlem", "Haarlem", "NL"),
    (r"De Doelen,? Rotterdam", "De Doelen", "Rotterdam", "NL"),
    (r"DNK Assen", "DNK", "Assen", "NL"),
    (r"Utrecht Cloud 9", "TivoliVredenburg, Cloud Nine", "Utrecht", "NL"),
    (r"Leiden Stadsgehoorzaal", "Stadsgehoorzaal Leiden", "Leiden", "NL"),
    (r"Nijmegen Vereeniging", "Concertgebouw De Vereeniging", "Nijmegen", "NL"),
    (r"The Hague Amare", "Amare", "The Hague", "NL"),
    (r"Royal Academy London", "Royal Academy of Music", "London", "GB"),
    (r"Warsaw Philharmonic", "Warsaw Philharmonic", "Warsaw", "PL"),
)


def _normalise_title(value):
    value = BeautifulSoup(html.unescape(value or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\W+", " ", value, flags=re.UNICODE).strip().casefold()


def _unfold_ical(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _calendar_events(text):
    events = []
    for block in _unfold_ical(text).split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT", 1)[0]
        fields = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.split(";", 1)[0]] = value.strip()

        start = fields.get("DTSTART", "")[:8]
        end = fields.get("DTEND", "")[:8]
        title = html.unescape(fields.get("SUMMARY", "")).strip()
        try:
            event_date = datetime.strptime(start, "%Y%m%d").date()
            end_date = datetime.strptime(end, "%Y%m%d").date()
        except ValueError:
            continue

        # Multi-day rows on this source are festival/season overviews rather
        # than individual performances. Invalid reversed ranges also occur.
        if not title or event_date != end_date:
            continue
        events.append((title, event_date.isoformat()))
    return events


def _location(title):
    for pattern, venue, city, country_code in VENUE_RULES:
        if re.search(pattern, title, re.IGNORECASE):
            return venue, city, country_code
    return None


def _get_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                "categories": SCHEDULE_CATEGORY_ID,
                "per_page": 100,
                "page": page,
                "orderby": "date",
                "order": "asc",
                "_fields": "id,link,title,content",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        page_posts = response.json()
        posts.extend(page_posts)
        total_pages = int(response.headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
    return posts


class BramVanSambeekCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="bramvansambeek_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NL",
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "ClassicalBot/1.0"})

        log_message("Fetching schedule posts", event="crawler_url_fetch", url=API_URL)
        posts = _get_posts(session)
        log_message("Fetching schedule calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        calendar_response = session.get(CALENDAR_URL, timeout=TIMEOUT)
        calendar_response.raise_for_status()

        posts_by_title = defaultdict(deque)
        for post in posts:
            posts_by_title[_normalise_title(post["title"]["rendered"])].append(post)

        records = []
        for title, event_date in _calendar_events(calendar_response.text):
            matching_posts = posts_by_title.get(_normalise_title(title))
            location = _location(title)
            if not matching_posts or not location:
                continue

            post = matching_posts.popleft()
            venue, city, country_code = location
            content = BeautifulSoup(post.get("content", {}).get("rendered", ""), "html.parser")
            detail_text = content.get_text("\n", strip=True)
            description = "\n\n".join(part for part in (title, detail_text) if part) or None
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": post["link"],
                    "time_from": None,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                }
            )

        log_message(
            "Schedule parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    BramVanSambeekCrawler().run()


if __name__ == "__main__":
    main()
