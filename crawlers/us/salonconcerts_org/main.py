import re
from datetime import datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Salon Concerts"
SOURCE_URL = "https://www.salonconcerts.org/"
TIMEOUT = 30

MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)
OCCURRENCE_RE = re.compile(
    rf"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*"
    rf"(?P<date>(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\s*,?\s+\d{{4}})"
    rf"[^\n]{{0,45}}?\b(?:at\s+)?(?P<time>\d{{1,2}}(?::\d{{2}})?\s*[ap]\.?m\.?)\b",
    re.IGNORECASE,
)

EXCLUDED_PATH_PARTS = (
    "artist-bio",
    "address-map",
    "concert-series-tickets",
    "tickets",
    "photos",
    "virtual-concert",
)


def _get(session, url):
    log_message("Fetching Salon Concerts page", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def _candidate_urls(sitemap_text):
    root = ElementTree.fromstring(sitemap_text)
    urls = []
    for node in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
        location = node.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        if not location:
            continue
        path = urlparse(location).path.lower().strip("/")
        if (
            "concert" in path
            and not any(part in path for part in EXCLUDED_PATH_PARTS)
            and location.startswith(SOURCE_URL)
        ):
            urls.append(location.rstrip("/"))
    return list(dict.fromkeys(urls))


def _clean_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup.select("script, style, noscript"):
        unwanted.decompose()
    return re.sub(r"\n\s*\n+", "\n", soup.get_text("\n", strip=True))


def _title(soup, url):
    headings = [
        heading.get_text(" ", strip=True)
        for heading in soup.select("h1, h2")
        if heading.get_text(" ", strip=True)
    ]
    for heading in headings:
        if "concert" in heading.lower() and "season" not in heading.lower():
            return re.sub(r"\s+", " ", heading)
    if headings:
        return re.sub(r"\s+", " ", headings[0])
    return urlparse(url).path.strip("/").replace("-", " ").title()


def _parse_date(value):
    value = re.sub(r"(\d)(?:st|nd|rd|th)", r"\1", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value.replace(",", "")).strip()
    return datetime.strptime(value, "%B %d %Y").date().isoformat()


def _parse_time(value):
    value = value.lower().replace(".", "").replace(" ", "")
    if ":" not in value:
        value = value[:-2] + ":00" + value[-2:]
    return datetime.strptime(value, "%I:%M%p").time().strftime("%H:%M")


def _parse_detail(payload, url):
    html = payload.get("mainContent") or ""
    soup = BeautifulSoup(html, "html.parser")
    description = _clean_text(html)
    lowered = description.lower()
    if (
        not description
        or "concert" not in lowered
        or "virtual concert" in lowered
        or "streamed concert" in lowered
        or "live stream date" in lowered
    ):
        return []

    title = _title(soup, url)
    records = []
    for occurrence in OCCURRENCE_RE.finditer(description):
        records.append(
            {
                "title": title,
                "date": _parse_date(occurrence.group("date")),
                "url": url,
                "time_from": _parse_time(occurrence.group("time")),
                "venue": "Private residence",
                "city": "Austin",
                "description": description,
            }
        )
    return records


class SalonConcertsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="salonconcerts_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalBot/1.0)"
        sitemap = _get(session, f"{SOURCE_URL}sitemap.xml").text
        urls = _candidate_urls(sitemap)
        records = []
        for url in urls:
            try:
                payload = _get(session, f"{url}?format=json").json()
                records.extend(_parse_detail(payload, url))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    "Failed to parse Salon Concerts detail",
                    event="crawler_item_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message(
            "Parsed Salon Concerts occurrences",
            event="crawler_parse_completed",
            record_count=len(records),
        )
        return records


def main():
    SalonConcertsCrawler().run()


if __name__ == "__main__":
    main()
