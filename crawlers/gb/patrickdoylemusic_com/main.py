import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Patrick Doyle"
SOURCE_URL = "https://patrickdoylemusic.com/"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/posts"

# News articles do not expose structured locations.  Only accept venues whose
# city and country are unambiguous; new locations are skipped until the source
# supplies enough information to resolve them safely.
KNOWN_VENUES = {
    "Golden Hall of the Musikverein": ("Vienna", "AT"),
    "Westminster Abbey": ("London", "GB"),
}

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
DATE_RE = re.compile(
    r"\b(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(" + "|".join(MONTHS) + r")\s+(\d{4})\b",
    re.IGNORECASE,
)


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = unescape(BeautifulSoup(value, "html.parser").get_text(" "))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def extract_date(description: str) -> str | None:
    match = DATE_RE.search(description)
    if not match:
        return None
    day, month_name, year = match.groups()
    try:
        return date(int(year), MONTHS[month_name.lower()], int(day)).isoformat()
    except ValueError:
        return None


def extract_location(text: str) -> tuple[str, str, str] | None:
    folded = text.casefold()
    for venue, (city, country_code) in KNOWN_VENUES.items():
        if venue.casefold() in folded:
            return venue, city, country_code
    return None


class PatrickDoyleCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="patrickdoylemusic_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GB",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "venue"],
    )

    def scrape(self) -> list[dict]:
        records = []
        page = 1
        total_pages = 1

        with requests.Session() as session:
            while page <= total_pages:
                params = {"per_page": 100, "page": page, "_fields": "id,link,title"}
                log_message(
                    "Fetching news API page",
                    event="crawler_url_fetch",
                    url=API_URL,
                    page=page,
                )
                response = session.get(API_URL, params=params, timeout=30)
                response.raise_for_status()
                total_pages = int(response.headers.get("X-WP-TotalPages", "1"))

                for post in response.json():
                    record = self._fetch_and_parse_post(session, post)
                    if record is not None:
                        records.append(record)
                page += 1

        log_message("Events parsed", event="crawler_records_parsed", record_count=len(records))
        return records

    @staticmethod
    def _fetch_and_parse_post(session: requests.Session, post: dict) -> dict | None:
        url = post.get("link")
        if not url:
            return None

        log_message("Fetching news detail", event="crawler_url_fetch", url=url)
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                "News detail fetch failed",
                event="crawler_item_failed",
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        paragraphs = [clean_text(node.decode_contents()) for node in soup.select("main p")]
        # The theme emits a misleading fixed display date before every article.
        # The longest paragraph is the article body and contains the real event
        # date when the post describes a concrete performance.
        description = max((text for text in paragraphs if text), key=len, default=None)
        title = clean_text((post.get("title") or {}).get("rendered"))
        if not (title and description):
            return None

        event_date = extract_date(description)
        location = extract_location(f"{title} {description}")
        if not event_date or not location:
            return None

        venue, city, country_code = location
        return {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": None,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }


def main():
    PatrickDoyleCrawler().run()


if __name__ == "__main__":
    main()
