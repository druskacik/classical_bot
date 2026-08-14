import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Bird On The Wire"
SOURCE_URL = "https://www.birdonthewire.net/"
SANITY_URL = "https://i46ayth5.api.sanity.io/v2022-03-07/data/query/production"
LONDON_TZ = ZoneInfo("Europe/London")
PAGE_SIZE = 500

# Bird On The Wire is a London promoter. Non-London shows consistently identify
# their city in the venue name or address; otherwise London is the defensible
# default used by the site's own listings.
UK_CITIES = (
    "Belfast", "Bexhill-on-Sea", "Birmingham", "Brighton", "Bristol",
    "Cambridge", "Cardiff", "Edinburgh", "Glasgow", "Leeds", "Liverpool",
    "London", "Manchester", "Margate", "Newcastle", "Norwich", "Nottingham",
    "Oxford", "Sheffield", "Southampton",
)
FOREIGN_MARKERS = (
    "Amsterdam", "Barcelona", "Berlin", "Brussels", "Copenhagen", "Dublin",
    "Lisbon", "Paris", "Prague", "Stockholm", "Vienna",
)
VENUE_CITY_OVERRIDES = {
    "Deaf Institute": "Manchester",
    "Fiddlers": "Bristol",
    "Mono": "Glasgow",
}
FOREIGN_VENUES = {"Flèche d'Or", "Kreuzberg Festsaal", "L'International", "Marie Antoinette (Berlin)"}


def _portable_text(blocks):
    paragraphs = []
    for block in blocks or []:
        text = "".join(
            child.get("text", "")
            for child in block.get("children", [])
            if isinstance(child, dict)
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs) or None


def _city_for(venue):
    venue_name = (venue.get("name") or "").strip()
    if venue_name.lower() == "online" or venue_name in FOREIGN_VENUES:
        return None
    if venue_name in VENUE_CITY_OVERRIDES:
        return VENUE_CITY_OVERRIDES[venue_name]
    evidence = "\n".join((venue_name, venue.get("address") or ""))
    if any(re.search(rf"\b{re.escape(city)}\b", evidence, re.I) for city in FOREIGN_MARKERS):
        return None
    for city in UK_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", evidence, re.I):
            return city
    return "London"


def _fetch_page(start):
    end = start + PAGE_SIZE
    query = f'''*[_type == "event" && defined(slug.current) && count(dates) > 0]
        | order(_id asc)[{start}...{end}]{{
          _id, title, "slug": slug.current, dates,
          description, supportCustom,
          "artistName": artist->name,
          "supportNames": support[]->name,
          "venue": venue->{{name, address}}
        }}'''
    log_message(
        "Fetching Sanity event page",
        event="crawler_url_fetch",
        url=SANITY_URL,
        page_start=start,
    )
    response = requests.get(SANITY_URL, params={"query": query}, timeout=60)
    response.raise_for_status()
    return response.json().get("result", [])


class BirdOnTheWireCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="birdonthewire_net",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GB",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        events = []
        start = 0
        while True:
            page = _fetch_page(start)
            events.extend(page)
            if len(page) < PAGE_SIZE:
                break
            start += PAGE_SIZE

        records = []
        for event in events:
            venue_data = event.get("venue") or {}
            venue = (venue_data.get("name") or "").strip()
            city = _city_for(venue_data) if venue else None
            slug = event.get("slug")
            title = (event.get("artistName") or event.get("title") or "").strip()
            if not (slug and title and venue and city):
                continue

            body = _portable_text(event.get("description"))
            extras = [event.get("supportCustom")]
            support_names = [name for name in event.get("supportNames") or [] if name]
            if support_names:
                extras.append("Support: " + ", ".join(support_names))
            description_parts = [part.strip() for part in extras if isinstance(part, str) and part.strip()]
            if body:
                description_parts.append(body)
            description = "\n\n".join(description_parts) or None
            url = f"https://www.birdonthewire.net/events/{slug}"

            for occurrence in event.get("dates") or []:
                raw_date = occurrence.get("date")
                if not raw_date:
                    continue
                try:
                    instant = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    local = instant.astimezone(LONDON_TZ)
                except (TypeError, ValueError):
                    continue
                records.append({
                    "title": title,
                    "date": local.date().isoformat(),
                    "url": url,
                    "time_from": local.time().replace(tzinfo=None).isoformat(timespec="minutes"),
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "description": description,
                })

        log_message(
            "Parsed Bird On The Wire events",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    BirdOnTheWireCrawler().run()


if __name__ == "__main__":
    main()
