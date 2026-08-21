from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "John Storgårds"
SOURCE_URL = "https://johnstorgards.com/"
CALENDAR_URLS = (
    urljoin(SOURCE_URL, "calendar"),
    urljoin(SOURCE_URL, "past-calendar"),
)
COUNTRY_CODES = {
    "Canada": "CA",
    "China": "CN",
    "Finland": "FI",
    "Germany": "DE",
    "Iceland": "IS",
    "Ireland": "IE",
    "Japan": "JP",
    "Norway": "NO",
    "Portugal": "PT",
    "United Kingdom": "GB",
    "United States": "US",
}


def _text(item, selector):
    element = item.select_one(selector)
    return element.get_text("\n", strip=True) if element else None


def _location(value):
    if not value or "," not in value:
        return None
    city, country = (part.strip() for part in value.rsplit(",", 1))
    country_code = COUNTRY_CODES.get(country)
    if not city or not country_code:
        return None
    # The calendar names this Helsinki park rather than its municipality.
    if city == "Tokoinranta" and country_code == "FI":
        city = "Helsinki"
    return city, country_code


def _time(value):
    if not value:
        return None
    return datetime.strptime(value.replace(" ", ""), "%I:%M%p").strftime("%H:%M:%S")


def _description(item):
    sections = []
    for label, selector in (
        ("Programme", ".concert-programm"),
        ("Orchestra", ".concert-orchestras"),
        ("Soloists", ".concert-soloists"),
    ):
        value = _text(item, selector)
        if value:
            sections.append(f"{label}:\n{value}")
    return "\n\n".join(sections) or None


class JohnStorgardsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="johnstorgards_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="FI",
        upload_target="classical",
        dedupe_subset=["date", "time_from", "venue", "title"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "classical-bot/1.0"})
        records = []

        for first_url in CALENDAR_URLS:
            page_url = first_url
            while page_url:
                log_message("Fetching calendar page", event="crawler_url_fetch", url=page_url)
                try:
                    response = session.get(page_url, timeout=30)
                    response.raise_for_status()
                except requests.RequestException as error:
                    log_message(
                        "Calendar page fetch failed",
                        event="crawler_url_fetch_failed",
                        url=page_url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    raise

                soup = BeautifulSoup(response.content, "html.parser")
                for item in soup.select(".concert-item"):
                    location = _location(_text(item, ".concert-title"))
                    venue = _text(item, ".concert-venue")
                    date_text = _text(item, ".concert-date")
                    if not location or not venue or not date_text:
                        continue

                    city, country_code = location
                    date = datetime.strptime(date_text, "%B %d, %Y").date().isoformat()
                    orchestra = _text(item, ".concert-orchestras")
                    title = f"John Storgårds – {orchestra or venue}"
                    link = item.select_one(".concert-link a[href]")
                    event_url = urljoin(page_url, link["href"]) if link else page_url

                    records.append(
                        {
                            "title": title,
                            "date": date,
                            "url": event_url,
                            "time_from": _time(_text(item, ".concert-start_time")),
                            "venue": venue,
                            "city": city,
                            "country_code": country_code,
                            "description": _description(item),
                        }
                    )

                next_link = soup.select_one("a.next.page-numbers[href]")
                page_url = urljoin(page_url, next_link["href"]) if next_link else None

        return records


def main():
    JohnStorgardsCrawler().run()


if __name__ == "__main__":
    main()
