import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html import unescape
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Beneath a Tree - Baroque to Folk"
SOURCE_URL = "https://www.beneathatree-baroquetofolk.com/"
COLLECTION_ID = "5d9fa24b66d51a68bc5b9d80"
API_URL = f"{SOURCE_URL}api/open/GetItemsByMonth"
SITE_TIMEZONE = ZoneInfo("America/Los_Angeles")
FIRST_CALENDAR_YEAR = 2017
FUTURE_YEARS = 5


def _plain_text(value):
    if not value:
        return None
    soup = BeautifulSoup(unescape(value), "html.parser")
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _city_from_location(location):
    """Return the locality, never an address or the venue name."""
    for key in ("addressLine2", "addressLine1"):
        value = (location.get(key) or "").strip()
        if not value:
            continue
        # Squarespace normally stores this as "City, ST, postal code".
        parts = [part.strip() for part in value.split(",")]
        if len(parts) >= 2 and parts[0]:
            return parts[0]
    return None


def _fetch_month(session, year, month):
    response = session.get(
        API_URL,
        params={"month": f"{month:02d}-{year}", "collectionId": COLLECTION_ID},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _fetch_description(session, url, fallback):
    try:
        log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
        response = session.get(url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        article = soup.select_one("main article") or soup.select_one("article")
        if article:
            # Remove event metadata and sharing controls while retaining programme text.
            for node in article.select("h1, time, .eventitem-meta, .eventitem-sourceurl, .eventitem-backlink, script, style"):
                node.decompose()
            text = article.get_text("\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if text:
                return text
    except requests.RequestException as error:
        log_message(
            "Concert detail fetch failed",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
    return fallback


class BeneathATreeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="beneathatree_baroquetofolk_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"})

        events = {}
        final_year = date.today().year + FUTURE_YEARS
        for year in range(FIRST_CALENDAR_YEAR, final_year + 1):
            for month in range(1, 13):
                try:
                    items = _fetch_month(session, year, month)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        "Calendar month fetch failed",
                        event="crawler_url_fetch_failed",
                        url=API_URL,
                        year=year,
                        month=month,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    raise
                for item in items:
                    event_id = item.get("id")
                    if event_id:
                        events[event_id] = item

        records = []
        description_jobs = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            for item in events.values():
                title = unescape(item.get("title") or "").strip()
                full_url = item.get("fullUrl")
                location = item.get("location") or {}
                venue = (location.get("addressTitle") or "").strip()
                city = _city_from_location(location)
                start_ms = item.get("startDate") or (item.get("structuredContent") or {}).get("startDate")
                if not (title and full_url and venue and city and start_ms):
                    continue

                start = datetime.fromtimestamp(start_ms / 1000, tz=SITE_TIMEZONE)
                end_ms = item.get("endDate") or (item.get("structuredContent") or {}).get("endDate")
                end = datetime.fromtimestamp(end_ms / 1000, tz=SITE_TIMEZONE) if end_ms else None
                url = requests.compat.urljoin(SOURCE_URL, full_url)
                fallback = _plain_text(item.get("body") or item.get("excerpt"))
                record = {
                    "title": title,
                    "date": start.date().isoformat(),
                    "url": url,
                    "time_from": start.strftime("%H:%M:%S"),
                    "time_to": end.strftime("%H:%M:%S") if end else None,
                    "venue": venue,
                    "city": city,
                    "description": fallback,
                }
                records.append(record)
                description_jobs[executor.submit(_fetch_description, session, url, fallback)] = record

            for future in as_completed(description_jobs):
                description_jobs[future]["description"] = future.result()

        log_message(
            "Calendar scrape completed",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    BeneathATreeCrawler().run()


if __name__ == "__main__":
    main()
