import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.geraldfinley.com/"
EVENT_DIRECTORY_URL = "https://www.geraldfinley.com/event-directory/"
SOURCE = "Gerald Finley"

# EventON does not consistently publish addresses.  These are first-party venue
# names used by the calendar, with their unambiguous cities and countries.
VENUE_GEOGRAPHY = {
    "Royal Albert Hall": ("London", "GB"),
    "Metropolitan Opera House": ("New York", "US"),
    "Concertgebouw Amsterdam": ("Amsterdam", "NL"),
    "Elbphilharmonie Hamburg, Großer Saal": ("Hamburg", "DE"),
    "Kölner Philharmonie": ("Cologne", "DE"),
    "Brucknerhaus Linz": ("Linz", "AT"),
    "Musikverein Wien": ("Vienna", "AT"),
    "Béla Bartók National Concert Hall": ("Budapest", "HU"),
    "Royal Festival Hall - Southbank Centre": ("London", "GB"),
    "Wiener Konzerthaus": ("Vienna", "AT"),
    "Wigmore Hall": ("London", "GB"),
    "Oslo Opera House": ("Oslo", "NO"),
    "Royal Opera House": ("London", "GB"),
    "Olavshallen Concert Hall": ("Trondheim", "NO"),
    "Wiener Staatsoper": ("Vienna", "AT"),
    "Bayerische Staatsoper": ("Munich", "DE"),
    "Symphony Center": ("Chicago", "US"),
    "Festspielhaus Erl": ("Erl", "AT"),
    "Isarphilharmonie": ("Munich", "DE"),
    "Basilika Ottobeuren": ("Ottobeuren", "DE"),
}


def _clean_text(node):
    if node is None:
        return None
    text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    return text or None


def _inferred_venue(title, published_venue):
    if published_venue:
        return published_venue
    title_lower = title.lower()
    if "oslo opera house" in title_lower:
        return "Oslo Opera House"
    if "tiroler festspiele erl" in title_lower:
        return "Festspielhaus Erl"
    if "ottobeuren" in title_lower:
        return "Basilika Ottobeuren"
    if "munich philharmonic" in title_lower:
        return "Isarphilharmonie"
    return None


def _description(event):
    # EventON duplicates some detail blocks for desktop and mobile.  Taking the
    # first full-description block avoids repeating the programme/personnel.
    detail = event.select_one(".eventon_full_description")
    text = _clean_text(detail)
    if text and text.lower() != "event details":
        return re.sub(r"^Event Details\s*", "", text, flags=re.IGNORECASE) or None
    return None


class GeraldFinleyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="geraldfinley_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        log_message("Fetching event directory", event="crawler_url_fetch", url=EVENT_DIRECTORY_URL)
        response = requests.get(EVENT_DIRECTORY_URL, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        for event in soup.select(".eventon_list_event"):
            title = _clean_text(event.select_one(".evcal_event_title"))
            url_node = event.select_one('.evo_event_schema [itemprop="url"]')
            date_node = event.select_one('.evo_event_schema [itemprop="startDate"]')
            if not title or url_node is None or date_node is None:
                continue

            start_value = date_node.get("content", "")
            try:
                date = datetime.strptime(start_value.split("T", 1)[0], "%Y-%m-%d").date().isoformat()
            except ValueError:
                log_message(
                    "Skipping event with invalid date",
                    event="crawler_record_skipped",
                    url=url_node.get("href"),
                )
                continue

            venue = _inferred_venue(title, _clean_text(event.select_one(".evo_location_name")))
            geography = VENUE_GEOGRAPHY.get(venue)
            if not venue or not geography:
                log_message(
                    "Skipping event without defensible venue geography",
                    event="crawler_record_skipped",
                    url=url_node.get("href"),
                )
                continue

            time_node = event.select_one(".evo_start .time")
            time_from = None
            if time_node:
                try:
                    time_from = datetime.strptime(time_node.get_text(strip=True), "%I:%M %p").strftime("%H:%M")
                except ValueError:
                    pass

            city, country_code = geography
            records.append(
                {
                    "title": title,
                    "date": date,
                    "url": url_node.get("href"),
                    "time_from": time_from,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": _description(event),
                }
            )

        log_message(
            "Parsed event directory",
            event="crawler_parse_completed",
            url=EVENT_DIRECTORY_URL,
            record_count=len(records),
        )
        return records


def main():
    GeraldFinleyCrawler().run()


if __name__ == "__main__":
    main()
