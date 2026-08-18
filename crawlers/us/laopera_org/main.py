import html
import json
from datetime import datetime
from urllib.parse import urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "LA Opera"
SOURCE_URL = "https://www.laopera.org/"
CALENDAR_URL = urljoin(SOURCE_URL, "whats-on/by-date")
ALGOLIA_URL = "https://n660y9i1f8-1.algolianet.com/1/indexes/*/queries"
ALGOLIA_APP_ID = "N660Y9I1F8"
ALGOLIA_API_KEY = "941081c330d3700e385d5d1a53ad150a"
ALGOLIA_INDEX = "prod_laopera_calendar"
PAGE_SIZE = 100
LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
}

# The calendar occasionally omits a city even though its venue identifies it.
# These are all first-party LA Opera performance venues represented in the feed.
VENUE_CITIES = {
    "dorothy chandler pavilion": "Los Angeles",
    "grand hall": "Los Angeles",
    "the united theater on broadway": "Los Angeles",
    "walt disney concert hall": "Los Angeles",
    "zipper hall": "Los Angeles",
    "the wallis": "Beverly Hills",
}


def _clean_text(value):
    if not value:
        return None
    text = BeautifulSoup(html.unescape(str(value)), "html.parser").get_text(" ", strip=True)
    return " ".join(text.split()) or None


def _valid_detail_url(url):
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"www.laopera.org", "laopera.org"}
        and parsed.path.startswith("/performances/")
    )


def _query_params(page):
    return urlencode(
        {
            "facets": json.dumps(["Genre"]),
            "filters": "ExcludeFromCalendar:false AND ItemType:Performance",
            "hitsPerPage": PAGE_SIZE,
            "page": page,
        }
    )


def _detail_data(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.select_one("main")
    if main is None:
        return None, None
    for element in main.select("script, style, noscript"):
        element.decompose()
    heading = main.select_one("h1")
    title = _clean_text(heading.get_text(" ", strip=True)) if heading else None
    description = _clean_text(main.get_text("\n", strip=True))
    return title, description


class LaOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="laopera_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "venue",
            "city",
            "country_code",
            "description",
        ],
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        api_headers = {
            "X-Algolia-Application-Id": ALGOLIA_APP_ID,
            "X-Algolia-API-Key": ALGOLIA_API_KEY,
        }

        hits = []
        page = 0
        while True:
            response = session.post(
                ALGOLIA_URL,
                headers=api_headers,
                json={
                    "requests": [
                        {"indexName": ALGOLIA_INDEX, "params": _query_params(page)}
                    ]
                },
                timeout=45,
            )
            response.raise_for_status()
            result = response.json()["results"][0]
            hits.extend(result.get("hits", []))
            if page + 1 >= result.get("nbPages", 0):
                break
            page += 1

        details = {}
        records = []
        for hit in hits:
            path = hit.get("KenticoUrl")
            url = urljoin(SOURCE_URL, path or "")
            venue = _clean_text(hit.get("Venue"))
            city = VENUE_CITIES.get((venue or "").casefold())
            timestamp = hit.get("StartDate")
            if not path or not _valid_detail_url(url) or not timestamp:
                continue

            if url not in details:
                try:
                    details[url] = _detail_data(session, url)
                except requests.RequestException as error:
                    log_message(
                        "Failed to fetch LA Opera performance details",
                        event="crawler_item_failed",
                        level="warning",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    details[url] = (None, None)
            detail_title, detail_description = details[url]

            # Open Door Days is listed without a venue in Algolia, while its
            # detail page explicitly welcomes attendees to this opera house.
            if not venue and detail_description and "Dorothy Chandler Pavilion" in detail_description:
                venue = "Dorothy Chandler Pavilion"
                city = "Los Angeles"

            title = detail_title or _clean_text(hit.get("Title"))
            if not title or not venue or not city:
                continue

            try:
                local_start = datetime.fromtimestamp(
                    float(timestamp) / 1000, tz=ZoneInfo("UTC")
                ).astimezone(LOCAL_TIMEZONE)
            except (TypeError, ValueError, OverflowError):
                continue

            records.append(
                {
                    "title": title,
                    "date": local_start.date().isoformat(),
                    "url": url,
                    "time_from": None
                    if hit.get("HideTime")
                    else local_start.strftime("%H:%M:%S"),
                    "venue": venue,
                    "city": city,
                    "country_code": "US",
                    "description": detail_description or _clean_text(hit.get("Desc")),
                }
            )

        log_message(
            "LA Opera calendar parsed",
            event="crawler_scrape_completed",
            url=CALENDAR_URL,
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda item: (item["date"], item["time_from"] or "", item["title"]),
        )


def main():
    LaOperaOrgCrawler().run()


if __name__ == "__main__":
    main()
