import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://sarasmiseth.com/"
SOURCE = "Sara Aimée Smiseth"
SITEMAP_URL = urljoin(SOURCE_URL, "wp-sitemap-posts-sc_event-1.xml")
REQUEST_TIMEOUT = 30


def _clean_text(value):
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _event_urls(session):
    log_message("Fetching event sitemap", event="crawler_url_fetch", url=SITEMAP_URL)
    response = session.get(SITEMAP_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "xml")
    return [loc.get_text(strip=True) for loc in soup.find_all("loc")]


def _description(main, title):
    details = main.select_one(".sc_event_details")
    if details:
        details.decompose()

    venue_heading = main.find(
        lambda tag: tag.name in {"h2", "h3", "h4", "h5"}
        and _clean_text(tag.get_text()) == "STED"
    )
    if venue_heading:
        for sibling in list(venue_heading.find_next_siblings()):
            sibling.decompose()
        venue_heading.decompose()

    text = main.get_text("\n", strip=True)
    if text.startswith(title):
        text = text[len(title):].lstrip()
    return _clean_text(text)


def _place(title, location, locality, description):
    title_venue = _clean_text(title.split("//", 1)[0])
    venue = _clean_text(location)
    city = _clean_text(locality)
    if city:
        city = re.sub(r"^\d{4}\s+", "", city)

    evidence = f"{title} {description or ''}".lower()
    if not venue and title_venue:
        if title_venue.lower() == "huskonsert oslo" and "skillebekk" in evidence:
            venue = "Huskonsert på Skillebekk"
        elif "konferans" not in title_venue.lower():
            venue = title_venue

    if not city:
        if "oslo" in evidence or title_venue == "Gamle Raadhus Scene":
            city = "Oslo"
        elif "sandefjord" in evidence:
            city = "Sandefjord"
        elif "grimstad" in evidence:
            city = "Grimstad"
        elif title_venue and title_venue.lower().startswith("ski "):
            city = "Ski"
        elif "trondheim" in evidence:
            city = "Trondheim"

    return venue, city


def _parse_event(session, url):
    log_message("Fetching event detail", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    page_title = _clean_text(soup.title.get_text() if soup.title else None)
    if not page_title:
        return None
    title = re.sub(r"\s+[–-]\s+SARA AIM[ÉE]+ SMISETH$", "", page_title, flags=re.I)

    date_time = soup.select_one(".sc-frontend-single-event__details__date time[datetime]")
    time_nodes = soup.select(".sc-frontend-single-event__details__time time[datetime]")
    main = soup.find("main")
    if not date_time or not main:
        return None

    start = datetime.fromisoformat(date_time["datetime"])
    time_from = start.strftime("%H:%M")
    time_to = None
    if len(time_nodes) > 1:
        time_to = datetime.fromisoformat(time_nodes[-1]["datetime"]).strftime("%H:%M")

    location_node = soup.select_one(
        ".sc-frontend-single-event__details__location "
        ".sc-frontend-single-event__details__val"
    )
    locality_node = soup.select_one(".tribe-locality")
    location = location_node.get_text(" ", strip=True) if location_node else None
    locality = locality_node.get_text(" ", strip=True) if locality_node else None
    description = _description(main, title)
    venue, city = _place(
        title,
        location,
        locality,
        description,
    )
    if not venue or not city:
        log_message(
            "Skipping event without a defensible venue or city",
            event="crawler_record_skipped",
            url=url,
        )
        return None

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": time_from,
        "time_to": time_to,
        "venue": venue,
        "city": city,
        "description": description,
    }


class SaraSmisethCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sarasmiseth_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NO",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "classical-bot/1.0"})
        records = []
        for url in _event_urls(session):
            try:
                record = _parse_event(session, url)
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch event detail",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            except (TypeError, ValueError) as error:
                log_message(
                    "Failed to parse event detail",
                    event="crawler_parse_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
        return records


def main():
    SaraSmisethCrawler().run()


if __name__ == "__main__":
    main()
