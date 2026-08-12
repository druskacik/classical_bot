from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "The Israeli Opera"
SOURCE_URL = "https://www.israel-opera.co.il/"
CALENDAR_API = (
    "https://www.israel-opera.co.il/wp-admin/admin-ajax.php"
    "?action=get_shows_calendar_events"
)
REQUEST_TIMEOUT = 30
DEFAULT_VENUE = "The Israeli Opera, Shlomo Lahat Opera House"
DEFAULT_CITY = "Tel Aviv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.7",
}


def _clean_text(value) -> str:
    return " ".join(unescape(str(value or "")).replace("\xa0", " ").split())


def _location(event: dict) -> tuple[str, str]:
    # The calendar is for the Opera House. The site's explicitly named Tzavta
    # series is the sole recurring off-site exception in the published feed.
    title = _clean_text(event.get("title"))
    subtitle = _clean_text(event.get("subtitle"))
    if "שרה בצוותא" in subtitle or title == "צוותא בת 70":
        return "Tzavta Theatre", DEFAULT_CITY
    return DEFAULT_VENUE, DEFAULT_CITY


def _fetch_description(url: str) -> str | None:
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        sections = []
        for node in soup.select(".single-show .elementor-widget-text-editor"):
            text = _clean_text(node.get_text(" ", strip=True))
            if text.startswith("היו ראשונים לקבל הטבות"):
                break
            if text:
                sections.append(text)
        description = "\n\n".join(dict.fromkeys(sections)).strip()
        return description or None
    except requests.RequestException as error:
        log_message(
            "Concert detail fetch failed",
            event="crawler_url_fetch_failed",
            level="warning",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


class IsraelOperaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="israel_opera_co_il",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="IL",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self) -> list[dict]:
        response = requests.get(CALENDAR_API, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        events = response.json()

        urls = {_clean_text(event.get("url")) for event in events if event.get("url")}
        descriptions = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_fetch_description, url): url for url in urls}
            for future in as_completed(futures):
                descriptions[futures[future]] = future.result()

        records = []
        for event in events:
            title = _clean_text(event.get("title"))
            url = _clean_text(event.get("url"))
            date = _clean_text(event.get("raw_date"))
            time_from = _clean_text(event.get("time_sort")) or None
            if not (title and url and date):
                log_message(
                    "Skipping calendar occurrence with incomplete core fields",
                    event="crawler_record_skipped",
                    level="warning",
                    post_id=event.get("post_id"),
                    url=url or None,
                )
                continue
            venue, city = _location(event)
            fallback = _clean_text(event.get("excerpt")) or None
            records.append(
                {
                    "title": title,
                    "date": date,
                    "url": url,
                    "time_from": time_from,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "description": descriptions.get(url) or fallback,
                }
            )
        return records


def main():
    IsraelOperaCrawler().run()


if __name__ == "__main__":
    main()
