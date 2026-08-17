import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Summerville Orchestra"
SOURCE_URL = "https://summervilleorchestra.org/"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/ajde_events"
EVENT_TYPE_IDS = "301,303,320,321"
TIMEOUT = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_text(value) -> str:
    if not value:
        return ""
    # WordPress sometimes nests entities (for example ``&amp;#038;``).
    text = html.unescape(html.unescape(str(value)))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch(session: requests.Session, url: str, **kwargs) -> requests.Response:
    response = session.get(url, timeout=TIMEOUT, **kwargs)
    response.raise_for_status()
    return response


def api_events(session: requests.Session) -> list[dict]:
    events = []
    page = 1
    while True:
        response = fetch(
            session,
            API_URL,
            params={
                "event_type": EVENT_TYPE_IDS,
                "per_page": 100,
                "page": page,
                "orderby": "id",
                "order": "asc",
                "_fields": "id,link,title,event_type",
            },
        )
        events.extend(response.json())
        total_pages = int(response.headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
    return events


def event_schemas(soup: BeautifulSoup) -> list[dict]:
    schemas = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        schemas.extend(item for item in values if isinstance(item, dict) and item.get("@type") == "Event")
    return schemas


def parse_datetime(value) -> tuple[str, str | None] | None:
    match = re.match(
        r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})"
        r"(?:T(?P<hour>\d{1,2}):(?P<minute>\d{2}))?",
        str(value or ""),
    )
    if not match:
        return None
    try:
        event_date = date(
            int(match.group("year")), int(match.group("month")), int(match.group("day"))
        ).isoformat()
    except ValueError:
        return None
    event_time = None
    if match.group("hour") is not None:
        hour, minute = int(match.group("hour")), int(match.group("minute"))
        if hour > 23 or minute > 59:
            return None
        event_time = f"{hour:02d}:{minute:02d}"
    return event_date, event_time


def city_from_address(address) -> str | None:
    if isinstance(address, dict):
        locality = clean_text(address.get("addressLocality"))
        if locality:
            return locality
        address = address.get("streetAddress")
    text = clean_text(address)
    match = re.search(r",\s*([^,]+?),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", text)
    return clean_text(match.group(1)) if match else None


def schema_location(value) -> tuple[str, str] | None:
    locations = value if isinstance(value, list) else [value]
    for location in locations:
        if not isinstance(location, dict):
            continue
        venue = clean_text(location.get("name"))
        city = city_from_address(location.get("address"))
        if venue and city:
            return venue, city
    return None


def location_from_description(value) -> tuple[str, str] | None:
    text = clean_text(value)
    if re.search(r"\bPublic Works Art Center\b", text, re.IGNORECASE) and re.search(
        r"\bin Summerville\b", text, re.IGNORECASE
    ):
        return "Public Works Art Center", "Summerville"
    return None


def parse_event(session: requests.Session, item: dict) -> list[dict]:
    page_url = item.get("link")
    if not page_url:
        return []
    soup = BeautifulSoup(fetch(session, page_url).text, "html.parser")
    records = []
    for schema in event_schemas(soup):
        parsed = parse_datetime(schema.get("startDate"))
        location = schema_location(schema.get("location")) or location_from_description(
            schema.get("description")
        )
        title = clean_text(schema.get("name") or (item.get("title") or {}).get("rendered"))
        if not parsed or not location or not title:
            continue
        event_date, time_from = parsed
        venue, city = location
        records.append(
            {
                "title": title,
                "date": event_date,
                "url": clean_text(schema.get("url")) or page_url,
                "time_from": time_from,
                "venue": venue,
                "city": city,
                "country_code": "US",
                "description": clean_text(schema.get("description")) or None,
                "source_url": SOURCE_URL,
                "source": SOURCE,
            }
        )
    return records


class SummervilleOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="summervilleorchestra_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        columns=[
            "title", "date", "url", "time_from", "venue", "city",
            "country_code", "description", "source_url", "source",
        ],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(HEADERS)
        items = api_events(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(parse_event, session, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Skipping unavailable Summerville Orchestra event",
                        event="crawler_item_failed",
                        level="warning",
                        url=item.get("link"),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        log_message(
            "Summerville Orchestra events parsed",
            event="crawler_parse_completed",
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda record: (record["date"], record["time_from"] or "", record["title"]),
        )


def main():
    SummervilleOrchestraOrgCrawler().run()


if __name__ == "__main__":
    main()
