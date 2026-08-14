import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Филхармонија на Република Северна Македонија"
SOURCE_URL = "https://www.filharmonija.mk/"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/events"
HOME_VENUE = "Концертна сала на Филхармонија"
HOME_CITY = "Скопје"

MONTHS = {
    "јануари": 1, "февруари": 2, "март": 3, "април": 4,
    "мај": 5, "јуни": 6, "јули": 7, "август": 8,
    "септември": 9, "октомври": 10, "oктомври": 10,
    "ноември": 11, "декември": 12,
}

# Names used by the site in touring-event titles and venue labels.
PLACES = (
    ("оберхаузен", "Оберхаузен", "DE"),
    ("бон", "Бон", "DE"),
    ("виена", "Виена", "AT"),
    ("софија", "Софија", "BG"),
    ("белград", "Белград", "RS"),
    ("охрид", "Охрид", "MK"),
    ("битола", "Битола", "MK"),
    ("струга", "Струга", "MK"),
    ("штип", "Штип", "MK"),
    ("тетово", "Тетово", "MK"),
    ("куманово", "Куманово", "MK"),
    ("гевгелија", "Гевгелија", "MK"),
    ("скопје", "Скопје", "MK"),
)


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def get_event_posts(session):
    posts = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={"per_page": 100, "page": page, "_fields": "id,link,title,season"},
            timeout=30,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
    return posts


def parse_date(date_text, season_text):
    match = re.search(r"(\d{1,2})\s+([A-Za-zА-Яа-яЀ-ӿ]+)", date_text)
    years = [int(year) for year in re.findall(r"20\d{2}", season_text)]
    if not match or not years:
        return None
    day = int(match.group(1))
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None
    start_year = years[0]
    end_year = years[-1] if len(years) > 1 else start_year + 1
    year = start_year if month >= 7 else end_year
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def resolve_location(title, venue):
    combined = f"{title} {venue}".lower()
    matches = [place for key, *place in PLACES if key in combined]
    touring = bool(re.search(r"гостува|турне|tour|guest", combined, re.I))
    if matches:
        # A page naming two cities represents a tour overview, not one occurrence.
        if len({city for city, _ in matches}) != 1:
            return None
        city, country_code = matches[0]
    elif touring:
        return None
    else:
        city, country_code = HOME_CITY, "MK"

    venue = clean_text(venue)
    named_venue = re.search(r"(НУ\s+Центар\s+за\s+култура)(?:\s*[–-]\s*[^,]+)?", title, re.I)
    if named_venue:
        venue = named_venue.group(1)
    if city == HOME_CITY and venue.lower() in {"концертна сала", "concert hall"}:
        venue = HOME_VENUE
    if not venue or venue.lower() == city.lower():
        return None
    return venue, city, country_code


def parse_event(session, post):
    url = post["link"]
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            "Skipping unavailable concert detail",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.content, "html.parser")
    header = soup.select_one(".event-header .desktop-media-tabs .single-post-content")
    if not header:
        return None
    metadata = [clean_text(node.get_text(" ", strip=True)) for node in header.select(".featured-post-date > *")]
    if len(metadata) < 4:
        return None

    title_node = header.select_one("h4")
    title = clean_text(title_node.get_text(" ", strip=True) if title_node else post["title"]["rendered"])
    event_date = parse_date(metadata[0], metadata[2])
    time_match = re.search(r"([01]?\d|2[0-3]):[0-5]\d", metadata[1])
    location = resolve_location(title, metadata[3])
    if not title or not event_date or not location:
        return None

    description_nodes = soup.select(".column")
    description = "\n".join(
        node.get_text("\n", strip=True) for node in description_nodes
    ).strip() or None
    venue, city, country_code = location
    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": time_match.group(0) if time_match else None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class FilharmonijaMkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="filharmonija_mk",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="MK",
        upload_target="classical",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers["User-Agent"] = "classical-concert-crawler/1.0"
        posts = get_event_posts(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(parse_event, session, post) for post in posts]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)
        records.sort(key=lambda record: (record["date"], record["title"], record["url"]))
        log_message(
            "Concert details parsed",
            event="crawler_details_parsed",
            record_count=len(records),
            source_record_count=len(posts),
        )
        return records


def main():
    FilharmonijaMkCrawler().run()


if __name__ == "__main__":
    main()
