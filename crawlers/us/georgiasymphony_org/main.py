import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Georgia Symphony Orchestra"
SOURCE_URL = "https://www.georgiasymphony.org/"
SITEMAP_URL = f"{SOURCE_URL}event-sitemap.xml"
ELIGIBLE_TYPES = {"Orchestra", "Chorus", "GYSO"}
VENUE_CITIES = {
    "Bailey Performance Center": "Kennesaw",
    "Bailey Performance Center at Kennesaw State University": "Kennesaw",
    "Dr. Bobbie Bailey & Family Performance Center": "Kennesaw",
    "Jennie T. Anderson Theatre": "Marietta",
    "Marietta Performing Arts Center": "Marietta",
    "Zion Baptist Church": "Marietta",
}


def _clean_text(element):
    if element is None:
        return None
    text = element.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def _detail_fields(soup):
    fields = {}
    for item in soup.select(".info_order li"):
        label = item.select_one(".label")
        if label is None:
            continue
        name = label.get_text(" ", strip=True).rstrip(":")
        label.extract()
        fields[name] = item.get_text(" ", strip=True)
    return fields


def _parse_time_field(value):
    normalized = value.replace("\xa0", " ")
    match = re.fullmatch(
        r"(.+? \d{1,2}, \d{4})\s*-\s*"
        r"(\d{1,2}:\d{2} [ap]m)(?:\s*-\s*(\d{1,2}:\d{2} [ap]m))?",
        normalized,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError(f"Unrecognized event time: {value!r}")
    event_date = datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
    time_from = datetime.strptime(match.group(2), "%I:%M %p").time().isoformat(timespec="minutes")
    time_to = None
    if match.group(3):
        time_to = datetime.strptime(match.group(3), "%I:%M %p").time().isoformat(timespec="minutes")
    return event_date, time_from, time_to


class GeorgiaSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="georgiasymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ClassicalBot/1.0 (+concert research)"})

    def _get_soup(self, url):
        log_message("Fetching crawler URL", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        parser = "xml" if url == SITEMAP_URL else "html.parser"
        return BeautifulSoup(response.content, parser)

    def _scrape_event(self, url):
        soup = self._get_soup(url)
        fields = _detail_fields(soup)
        event_types = {part.strip() for part in fields.get("Type", "").split(",")}
        if not event_types.intersection(ELIGIBLE_TYPES):
            return None

        title_node = soup.select_one(".single_event h1")
        venue = fields.get("Venue", "").strip()
        city = VENUE_CITIES.get(venue)
        if title_node is None or not venue or city is None or not fields.get("Time"):
            return None

        event_date, time_from, time_to = _parse_time_field(fields["Time"])
        return {
            "title": title_node.get_text(" ", strip=True),
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "time_to": time_to,
            "venue": venue,
            "city": city,
            "description": _clean_text(soup.select_one(".event_intro .content")),
        }

    def scrape(self):
        sitemap = self._get_soup(SITEMAP_URL)
        urls = [node.get_text(strip=True) for node in sitemap.select("loc")]
        records = []
        for url in urls:
            try:
                record = self._scrape_event(url)
                if record is not None:
                    records.append(record)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    "Failed to scrape event detail",
                    event="crawler_item_failed",
                    level="warning",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return records


def main():
    GeorgiaSymphonyCrawler().run()


if __name__ == "__main__":
    main()
