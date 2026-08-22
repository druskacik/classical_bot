import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Sofiane Pamart"
SOURCE_URL = "https://www.sofianepamart.com/"
TOUR_URL = "https://www.sofianepamart.com/pages/tour"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"}

# The English tour page uses country names, not ISO codes. These cover Sofiane
# Pamart's established touring territories while unknown locations are skipped.
COUNTRY_CODES = {
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "brazil": "BR",
    "canada": "CA",
    "china": "CN",
    "czech republic": "CZ",
    "denmark": "DK",
    "france": "FR",
    "germany": "DE",
    "greece": "GR",
    "ireland": "IE",
    "italy": "IT",
    "japan": "JP",
    "luxembourg": "LU",
    "mexico": "MX",
    "monaco": "MC",
    "morocco": "MA",
    "netherlands": "NL",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "south korea": "KR",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "united arab emirates": "AE",
    "united kingdom": "GB",
    "united states": "US",
    "usa": "US",
}


def clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def get_soup(url):
    log_message("Fetching page", event="crawler_url_fetch", url=url)
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def iter_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get("@type") == "Event":
                yield item
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                yield from (
                    child
                    for child in item["@graph"]
                    if isinstance(child, dict) and child.get("@type") == "Event"
                )


def detail_fields(url):
    try:
        soup = get_soup(url)
    except requests.RequestException as error:
        log_message(
            "Could not fetch event detail",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return {}

    event = next(iter_json_ld(soup), None)
    if event:
        location = event.get("location") or {}
        return {
            "title": clean_text(event.get("name")),
            "venue": clean_text(location.get("name")) if isinstance(location, dict) else None,
            "description": clean_text(BeautifulSoup(event.get("description") or "", "html.parser").get_text(" ")),
            # JSON-LD timestamps may be UTC while the concert time is local.
            # Without a dependable venue timezone, omitting it is safer.
            "time_from": None,
        }

    description = soup.select_one('meta[name="description"]')
    return {
        "description": clean_text(description.get("content")) if description else None,
    }


def parse_location(value):
    parts = [clean_text(part) for part in value.rsplit(",", 1)]
    if len(parts) != 2 or not all(parts):
        return None, None
    return parts[0], COUNTRY_CODES.get(parts[1].casefold())


class SofianePamartCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sofianepamart_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "url"],
    )

    def scrape(self):
        soup = get_soup(TOUR_URL)
        records = []
        for card in soup.select(".ticket_wrapper .ticket_content"):
            date_parts = [clean_text(node.get_text(" ")) for node in card.select(".ticket-left-one p")]
            event_parts = [clean_text(node.get_text(" ")) for node in card.select(".ticket-left-two p")]
            link = card.select_one("a[href]")
            if len(date_parts) < 2 or len(event_parts) < 2 or not link:
                continue

            try:
                event_date = datetime.strptime(f"{date_parts[0]} {date_parts[1]}", "%d %b %Y").date().isoformat()
            except (TypeError, ValueError):
                continue

            city, country_code = parse_location(event_parts[1])
            if not city or not country_code:
                continue

            url = link.get("href")
            details = detail_fields(url)
            venue = details.get("venue") or event_parts[0]
            if not details.get("venue") and "festival" in venue.casefold():
                continue
            if not venue:
                continue
            records.append({
                "title": details.get("title") or SOURCE,
                "date": event_date,
                "url": url,
                "time_from": details.get("time_from"),
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": details.get("description"),
            })

        return records


def main():
    SofianePamartCrawler().run()


if __name__ == "__main__":
    main()
