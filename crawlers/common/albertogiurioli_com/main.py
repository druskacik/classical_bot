import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Alberto Giurioli"
SOURCE_URL = "https://www.albertogiurioli.com/"
ARCHIVE_URLS = (
    urljoin(SOURCE_URL, "upcoming-events/"),
    urljoin(SOURCE_URL, "past-events/"),
)


def _session():
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers["User-Agent"] = "ClassicalBot/1.0 (+concert calendar crawler)"
    return session


def _get_soup(session, url):
    log_message("Fetching page", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def _event_fields(article):
    fields = {}
    for row in article.select("table tr"):
        icon = row.select_one("i")
        cells = row.select("td")
        if icon is None or len(cells) < 2:
            continue
        classes = set(icon.get("class", []))
        value = cells[-1].get_text(" ", strip=True)
        if "fa-calendar" in classes:
            fields["date"] = value
        elif "fa-clock" in classes:
            fields["time"] = value
        elif "fa-globe" in classes:
            fields["location"] = value
        elif "fa-arrow-down" in classes:
            fields["venue"] = value
    return fields


def _geography(location):
    normalized = re.sub(r"\s+", " ", location).strip()
    lower = normalized.lower()
    if "badia polesine" in lower:
        return "Badia Polesine", "IT"
    if "manchester" in lower:
        return "Manchester", "GB"
    if "london" in lower:
        return "London", "GB"
    if "paris" in lower:
        return "Paris", "FR"
    return None, None


def _description(article):
    paragraphs = [
        paragraph.get_text(" ", strip=True)
        for paragraph in article.select(".righthalf > p")
        if paragraph.get_text(" ", strip=True)
    ]
    return "\n".join(paragraphs) or None


def _parse_event(session, url):
    soup = _get_soup(session, url)
    article = soup.select_one("article.event")
    if article is None:
        log_message("Event detail is missing", event="crawler_parse_skip", url=url)
        return None

    title_node = article.select_one(".event-boldtitle")
    fields = _event_fields(article)
    city, country_code = _geography(fields.get("location", ""))
    venue = fields.get("venue", "").strip()
    if title_node is None or not fields.get("date") or not city or not country_code or not venue:
        log_message("Event is missing required fields", event="crawler_parse_skip", url=url)
        return None

    try:
        event_date = datetime.strptime(fields["date"], "%B %d, %Y").date().isoformat()
    except ValueError as error:
        log_message(
            "Invalid event date",
            event="crawler_parse_skip",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    time_from = None
    if fields.get("time"):
        try:
            time_from = datetime.strptime(fields["time"].lower(), "%I:%M %p").strftime("%H:%M")
        except ValueError:
            time_from = None

    return {
        "title": title_node.get_text(" ", strip=True),
        "date": event_date,
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": _description(article),
    }


class AlbertoGiurioliCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="albertogiurioli_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = _session()
        event_urls = []
        for archive_url in ARCHIVE_URLS:
            soup = _get_soup(session, archive_url)
            event_urls.extend(
                urljoin(SOURCE_URL, link["href"])
                for link in soup.select("a.event-link[href]")
            )

        records = []
        for url in dict.fromkeys(event_urls):
            record = _parse_event(session, url)
            if record is not None:
                records.append(record)
        return records


def main():
    AlbertoGiurioliCrawler().run()


if __name__ == "__main__":
    main()
