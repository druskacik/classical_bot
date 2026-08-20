import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Max Emanuel Cencic"
SOURCE_URL = "https://www.cencic.com/"
SCHEDULE_URL = f"{SOURCE_URL}schedule/"

COUNTRY_CODES = {
    "Deutschland": "DE",
    "Germany": "DE",
    "Österreich": "AT",
    "Austria": "AT",
    "Polen": "PL",
    "Poland": "PL",
}


def _session():
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "classical-concert-crawler/1.0"
    return session


def _parse_datetime(value):
    normalized = re.sub(r"\s+", " ", value).strip()
    date_text, separator, time_text = normalized.partition("|")
    parsed_date = datetime.strptime(date_text.strip(), "%A, %d %B %Y").date().isoformat()
    time_from = None
    if separator:
        time_from = datetime.strptime(time_text.strip(), "%H:%M").time().isoformat(timespec="minutes")
    return parsed_date, time_from


def _parse_event(article):
    title_node = article.select_one(".event-info h3")
    date_node = article.select_one(".event-long-date")
    place_nodes = article.select(".event-place span")
    link_node = article.select_one(".buy-link a[href]")
    if not title_node or not date_node or len(place_nodes) < 3 or not link_node:
        return None

    title = title_node.get_text(" ", strip=True)
    venue, city, country_name = [node.get_text(" ", strip=True) for node in place_nodes[:3]]
    country_code = COUNTRY_CODES.get(country_name)
    url = link_node.get("href", "").strip()
    if not all((title, venue, city, country_code, url)):
        return None

    date, time_from = _parse_datetime(date_node.get_text(" ", strip=True))
    return {
        "title": title,
        "date": date,
        "url": url,
        "time_from": time_from,
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": None,
    }


class CencicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="cencic_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching schedule", event="crawler_url_fetch", url=SCHEDULE_URL)
        response = _session().get(SCHEDULE_URL, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        skipped_count = 0
        for article in soup.select("article.e-post"):
            try:
                record = _parse_event(article)
            except ValueError as error:
                skipped_count += 1
                log_message(
                    "Skipping event with an invalid date or time",
                    event="crawler_record_skipped",
                    url=SCHEDULE_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record is None:
                skipped_count += 1
                continue
            records.append(record)

        if skipped_count:
            log_message(
                "Skipped incomplete schedule entries",
                event="crawler_records_skipped",
                url=SCHEDULE_URL,
                record_count=skipped_count,
            )
        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["url"]))
        return records


def main():
    CencicCrawler().run()


if __name__ == "__main__":
    main()
