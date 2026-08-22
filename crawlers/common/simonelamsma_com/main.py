from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Simone Lamsma"
SOURCE_URL = "https://simonelamsma.com/"
CONCERTS_URL = urljoin(SOURCE_URL, "concerts/")
PAST_CONCERTS_URL = urljoin(SOURCE_URL, "past-concerts/")

# The calendar uses display names rather than ISO codes. It also sometimes uses
# constituent countries/regions and contains two historic spelling mistakes.
COUNTRY_CODES = {
    "Argentina": "AR",
    "Australia": "AU",
    "Austria": "AT",
    "Belgium": "BE",
    "Brazil": "BR",
    "Canada": "CA",
    "Chili": "CL",
    "Croatia": "HR",
    "Denmark": "DK",
    "Estonia": "EE",
    "Finland": "FI",
    "France": "FR",
    "Germany": "DE",
    "Iceland": "IS",
    "Ireland": "IE",
    "Italy": "IT",
    "Japan": "JP",
    "Latvia": "LV",
    "Luxembourg": "LU",
    "Netherlands": "NL",
    "Norway": "NO",
    "Poland": "PL",
    "Portugal": "PT",
    "Romania": "RO",
    "Scotland": "GB",
    "Serbia": "RS",
    "Slovakia": "SK",
    "Slovenia": "SI",
    "South Korea": "KR",
    "Spain": "ES",
    "Sweden": "SE",
    "Switerland": "CH",
    "Switzerland": "CH",
    "Tasmania": "AU",
    "Tenerife": "ES",
    "The Netherlands": "NL",
    "Turkey": "TR",
    "UK": "GB",
    "Uruguay": "UY",
    "US": "US",
    "Wales": "GB",
}


def clean_text(element):
    if element is None:
        return None
    text = "\n".join(
        line.strip() for line in element.get_text("\n", strip=True).splitlines() if line.strip()
    )
    return text or None


def parse_time(value):
    if not value:
        return None
    normalized = value.replace(".", ":").replace(" ", "").upper()
    for pattern in ("%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(normalized, pattern).strftime("%H:%M")
        except ValueError:
            continue
    return None


def parse_event(element, page_url):
    raw_date = clean_text(element.select_one(".vsel-meta-date"))
    title = clean_text(element.select_one(".vsel-meta-title"))
    location = clean_text(element.select_one(".vsel-meta-location"))
    venue = clean_text(element.select_one(".vsel-meta-summary"))

    # Date ranges are festival/engagement overviews, not concrete occurrences.
    if not raw_date or raw_date.lower().startswith(("start:", "end:")):
        return None
    try:
        event_date = datetime.strptime(raw_date, "%d %b %Y").date().isoformat()
    except ValueError:
        return None

    # A broadcast listing is not a live public performance in project scope.
    if not title or "broadcast" in title.lower():
        return None
    if not location or "," not in location or not venue:
        return None

    country_name, city = (part.strip() for part in location.split(",", 1))
    country_code = COUNTRY_CODES.get(country_name)
    if not country_code or not city:
        return None

    description = clean_text(element.select_one(".vsel-info"))
    return {
        "title": title,
        "date": event_date,
        "url": page_url,
        "time_from": parse_time(clean_text(element.select_one(".vsel-meta-time"))),
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class SimoneLamsmaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="simonelamsma_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "classical-concert-crawler/1.0 (+https://classical.bot/)"}
        )

    def fetch_page(self, url):
        log_message("Fetching concert page", event="crawler_url_fetch", url=url)
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                "Concert page fetch failed",
                event="crawler_url_fetch_failed",
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return BeautifulSoup(response.content, "html.parser")

    def scrape_listing(self, initial_url, paginated=False):
        records = []
        url = initial_url
        visited = set()

        while url and url not in visited:
            visited.add(url)
            soup = self.fetch_page(url)
            for element in soup.select(".vsel-content"):
                record = parse_event(element, url)
                if record:
                    records.append(record)

            if not paginated:
                break
            next_link = next(
                (
                    link
                    for link in soup.select(".vsel-nav a[href]")
                    if "next" in link.get_text(" ", strip=True).lower()
                ),
                None,
            )
            if next_link is None:
                break
            candidate = urljoin(url, next_link["href"])
            parsed = urlparse(candidate)
            if parsed.netloc != urlparse(SOURCE_URL).netloc or not parsed.path.startswith(
                "/past-concerts/"
            ):
                break
            url = candidate

        return records

    def scrape(self):
        records = self.scrape_listing(CONCERTS_URL)
        records.extend(self.scrape_listing(PAST_CONCERTS_URL, paginated=True))
        return records


def main():
    SimoneLamsmaCrawler().run()


if __name__ == "__main__":
    main()
