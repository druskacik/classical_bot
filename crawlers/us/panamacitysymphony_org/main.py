import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Panama City Symphony"
SOURCE_URL = "https://panamacitysymphony.org/"
SCHEDULE_URL = urljoin(SOURCE_URL, "schedule/")
PAST_CONCERTS_URL = urljoin(SOURCE_URL, "past-concerts/")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

VENUE_CITIES = {
    "aaron bessant park": "Panama City Beach",
    "arnold high school": "Panama City Beach",
    "barbara w. nelson fine arts center": "Panama City",
    "barbara w nelson fine arts center": "Panama City",
    "captain anderson's event center": "Panama City Beach",
    "forest park church": "Panama City",
    "fsu-pc holley center": "Panama City",
    "helen blackburn auditorium": "Panama City Beach",
    "st andrew baptist church": "Panama City",
    "st. andrew baptist church": "Panama City",
}


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.get_text(" ", strip=True)).strip()


def parse_time(value):
    match = re.search(r"\|\s*(\d{1,2}:\d{2}\s*[AP]M)\b", value, re.IGNORECASE)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).upper().replace(" ", ""), "%I:%M%p").strftime("%H:%M")
    except ValueError:
        return None


def parse_dates(value):
    head = value.split("|", 1)[0].strip()
    match = re.match(
        r"([A-Z][a-z]{2})\s+(\d{1,2})(?:\s*&\s*(\d{1,2}))?,\s*(\d{4})",
        head,
    )
    if not match:
        return []
    month, first_day, second_day, year = match.groups()
    results = []
    for day in (first_day, second_day):
        if not day:
            continue
        try:
            results.append(datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date().isoformat())
        except ValueError:
            return []
    return results


def location_parts(value):
    location = value.strip(" ,")
    lower = location.casefold()
    if "panama city beach" in lower:
        city = "Panama City Beach"
    elif "panama city" in lower:
        city = "Panama City"
    else:
        city = next((name for venue, name in VENUE_CITIES.items() if venue in lower), "")

    venue = re.split(r",\s*Panama City(?: Beach)?,\s*Florida\b", location, flags=re.IGNORECASE)[0]
    venue = re.sub(r",\s*(?:Corner|at)\b.*$", "", venue, flags=re.IGNORECASE).strip(" ,")
    return venue, city


def current_records(soup):
    records = []
    for card in soup.select("div.content[date-year][date-month][date-day]"):
        heading = card.select_one("h2")
        title_link = card.select_one('h3 a[href*="/product/"]')
        if not heading or not title_link:
            continue

        dates = parse_dates(clean_text(heading))
        title = clean_text(title_link)
        url = urljoin(SOURCE_URL, title_link.get("href", ""))
        paragraphs = [clean_text(node) for node in card.select("p")]
        location = next(
            (
                text
                for text in paragraphs
                if any(venue in text.casefold() for venue in VENUE_CITIES)
            ),
            "",
        )
        if not location:
            location = next(
                (
                    text
                    for text in paragraphs
                    if re.search(r",\s*Panama City(?: Beach)?,\s*Florida\b", text, re.IGNORECASE)
                ),
                "",
            )
        venue, city = location_parts(location)
        if not dates or not title or not url or not venue or not city:
            log_message(
                "Skipping current event with incomplete required fields",
                event="crawler_event_skipped",
                level="warning",
                url=url or SCHEDULE_URL,
                has_date=bool(dates),
                has_venue=bool(venue),
                has_city=bool(city),
            )
            continue

        description_nodes = [node for node in card.select("h4, p") if clean_text(node) != location]
        description = "\n\n".join(filter(None, (clean_text(node) for node in description_nodes))) or None
        for event_date in dates:
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": parse_time(clean_text(heading)),
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "description": description,
                }
            )
    return records


def past_records(soup):
    records = []
    for card in soup.select("div.middle-section div.content-container"):
        date_heading = card.select_one("h3")
        title_heading = card.select_one("h4.main-font")
        first_paragraph = card.select_one("p")
        if not date_heading or not title_heading or not first_paragraph:
            continue

        dates = parse_dates(clean_text(date_heading))
        title = clean_text(title_heading)
        venue_node = first_paragraph.select_one("b, strong")
        venue = clean_text(venue_node)
        city = next((name for key, name in VENUE_CITIES.items() if key in venue.casefold()), "")
        link = card.find_parent("div", class_="middle-section").select_one('a[href*="/product/"]')
        url = urljoin(SOURCE_URL, link.get("href")) if link and link.get("href") else PAST_CONCERTS_URL
        if not dates or not title or not venue or not city:
            log_message(
                "Skipping past event with incomplete required fields",
                event="crawler_event_skipped",
                level="warning",
                url=url,
                has_date=bool(dates),
                has_venue=bool(venue),
                has_city=bool(city),
            )
            continue

        description = clean_text(first_paragraph)
        if description.startswith(venue):
            description = description[len(venue):].strip(" :-")
        for event_date in dates:
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": parse_time(clean_text(date_heading)),
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "description": description or None,
                }
            )
    return records


class PanamaCitySymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="panamacitysymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        records = []
        for url, parser in (
            (SCHEDULE_URL, current_records),
            (PAST_CONCERTS_URL, past_records),
        ):
            log_message("Fetching concert listing", event="crawler_url_fetch", url=url)
            response = session.get(url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            parsed = parser(BeautifulSoup(response.text, "html.parser"))
            records.extend(parsed)
            log_message(
                "Concert listing parsed",
                event="crawler_listing_parsed",
                url=url,
                record_count=len(parsed),
            )

        return sorted(records, key=lambda item: (item["date"], item["time_from"] or "", item["title"]))


def main():
    PanamaCitySymphonyOrgCrawler().run()


if __name__ == "__main__":
    main()
