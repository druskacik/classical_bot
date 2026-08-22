import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Noa Wildschut"
SOURCE_URL = "https://www.noawildschut.com/"
CALENDAR_URLS = (
    "https://www.noawildschut.com/concerts/",
    "https://www.noawildschut.com/concerts/past/",
)

COUNTRY_CODES = {
    "ARGENTINA": "AR",
    "AUSTRALIA": "AU",
    "AUSTRIA": "AT",
    "CANADA": "CA",
    "FRANCE": "FR",
    "GERMANY": "DE",
    "GREECE": "GR",
    "IRELAND": "IE",
    "ISRAEL": "IL",
    "ITALY": "IT",
    "LIECHTENSTEIN": "LI",
    "LUXEMBOURG": "LU",
    "NORWAY": "NO",
    "POLAND": "PL",
    "PORTUGAL": "PT",
    "ROMANIA": "RO",
    "SPAIN": "ES",
    "SWITZERLAND": "CH",
    "SWITZERLANDS": "CH",
    "THE NETHERLANDS": "NL",
    "UNITED KINGDOM": "GB",
}


def _text(element):
    return element.get_text("\n", strip=True) if element else None


def _country_code(title, address):
    match = re.search(r"\(([A-Z]{2,3})\)", title)
    if match:
        code = match.group(1)
        return "GB" if code == "UK" else code

    country = address.rsplit(",", 1)[-1].strip().upper()
    return COUNTRY_CODES.get(country)


def _parse_event(event):
    schema_link = event.select_one('.evo_event_schema a[itemprop="url"]')
    date_element = event.select_one('meta[itemprop="startDate"]')
    heading_element = event.select_one(".evcal_event_title")
    details_element = event.select_one(".eventon_desc_in")
    subtitle_element = event.select_one(".evcal_event_subtitle")
    location_element = event.select_one(".evcal_desc")

    url = schema_link.get("href") if schema_link else None
    raw_date = date_element.get("content") if date_element else None
    heading = _text(heading_element)
    venue = location_element.get("data-location_name") if location_element else None
    address = location_element.get("data-location_address") if location_element else None

    if not all((url, raw_date, heading, venue, address)):
        return None

    try:
        year, month, day = (int(part) for part in raw_date.split("-"))
        event_date = date(year, month, day).isoformat()
    except (TypeError, ValueError):
        return None

    city = address.rsplit(",", 1)[0].strip() if "," in address else None
    country_code = _country_code(heading, address)
    if not city or not country_code:
        return None

    title = _text(subtitle_element) or heading
    time_from = _text(event.select_one(".evo_start .time"))
    time_to = _text(event.select_one(".evo_end .time"))
    if time_from == "allday":
        time_from = None
    if time_to == "allday":
        time_to = None

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": time_from,
        "time_to": time_to,
        "venue": venue.strip(),
        "city": city,
        "country_code": country_code,
        "description": _text(details_element),
    }


class NoaWildschutCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="noawildschut_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        session = requests.Session()
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (compatible; ClassicalBot/1.0; +https://www.noawildschut.com/)"
        )

        for url in CALENDAR_URLS:
            log_message("Fetching concert calendar", event="crawler_url_fetch", url=url)
            response = session.get(url, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for event in soup.select(".eventon_list_event[data-event_id]"):
                record = _parse_event(event)
                if record:
                    records.append(record)

        log_message(
            "Concert calendars parsed",
            event="crawler_parse_completed",
            record_count=len(records),
        )
        return records


def main():
    NoaWildschutCrawler().run()


if __name__ == "__main__":
    main()
