from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import re
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Israel Philharmonic Orchestra"
SOURCE_URL = "https://www.ipo.co.il/"
CALENDAR_API = "https://www.ipo.co.il/wp-admin/admin-ajax.php"
EMPTY_MONTH_LIMIT = 12
REQUEST_TIMEOUT = 30

# These are the three halls documented on the orchestra's own "Halls" page.
# The calendar only supplies a city, so occurrences in other cities are skipped.
VENUES = {
    "תל אביב": "היכל התרבות ע\"ש צ'רלס ברונפמן, אולם לאוי",
    "תל-אביב": "היכל התרבות ע\"ש צ'רלס ברונפמן, אולם לאוי",
    "חיפה": "אודיטוריום חיפה",
    "ירושלים": "תיאטרון ירושלים, אולם שרובר",
}


def _month_offset(year: int, month: int, offset: int) -> tuple[int, int]:
    value = year * 12 + month - 1 + offset
    return divmod(value, 12)[0], divmod(value, 12)[1] + 1


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": (
                f"{SOURCE_URL}%D7%9C%D7%95%D7%97-%D7%A9%D7%A0%D7%94/"
            ),
        }
    )
    return session


def _fetch_month(session: requests.Session, year: int, month: int) -> list[dict]:
    response = session.get(
        CALENDAR_API,
        params={
            "action": "ajax_get_calendar_events",
            "month": month,
            "year": year,
            "calendar_type": "normal",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json().get("data", {})
    soup = BeautifulSoup(payload.get("list_events_html", ""), "html.parser")
    records = []
    for item in soup.select("li.loop-calendar-list-event[data-event_id]"):
        title_node = item.select_one(".ipo-program-details h4")
        url_node = item.select_one("a.overlay-link[href]")
        date_node = item.select_one(".ipo-list-left.desktop-only span.date")
        time_node = item.select_one(".ipo-list-left.desktop-only span.time")
        city = item.get("data-event_location", "").strip()
        venue = VENUES.get(city)
        if not (title_node and url_node and date_node and city and venue):
            log_message(
                "Skipping calendar occurrence with incomplete location or core fields",
                event="crawler_record_skipped",
                level="warning",
                event_id=item.get("data-event_id"),
                city=city or None,
            )
            continue
        raw_date = date_node.get_text(" ", strip=True)
        try:
            day, parsed_month, parsed_year = (int(part) for part in raw_date.split("."))
            parsed_date = date(parsed_year, parsed_month, day).isoformat()
        except (TypeError, ValueError):
            continue
        subtitle = item.select_one(".ipo-program-details p.text")
        records.append(
            {
                "title": title_node.get_text(" ", strip=True),
                "date": parsed_date,
                "url": url_node["href"],
                "time_from": time_node.get_text(" ", strip=True) if time_node else None,
                "time_to": None,
                "venue": venue,
                "city": city.replace("-", " "),
                "calendar_description": subtitle.get_text(" ", strip=True) if subtitle else None,
                "event_id": item.get("data-event_id"),
            }
        )
    return records


def _description_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _fetch_description(url: str) -> str | None:
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        sections = soup.select("main section.hero_area, main section.program-info")
        text = "\n".join(section.get_text("\n", strip=True) for section in sections)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text or None
    except (requests.RequestException, ValueError) as error:
        log_message(
            "Concert detail fetch failed",
            event="crawler_url_fetch_failed",
            level="warning",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


class IpoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="ipo_co_il",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="IL",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url"],
    )

    def scrape(self) -> list[dict]:
        today = date.today()
        records = []
        seen_event_ids = set()

        with _session() as session:
            for direction in (-1, 1):
                empty_months = 0
                offset = 0 if direction == -1 else 1
                while empty_months < EMPTY_MONTH_LIMIT:
                    year, month = _month_offset(today.year, today.month, offset)
                    month_records = _fetch_month(session, year, month)
                    empty_months = 0 if month_records else empty_months + 1
                    for record in month_records:
                        if record["event_id"] not in seen_event_ids:
                            seen_event_ids.add(record["event_id"])
                            records.append(record)
                    offset += direction

        description_urls = {_description_url(record["url"]) for record in records}
        descriptions = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_fetch_description, url): url for url in description_urls}
            for future in as_completed(futures):
                descriptions[futures[future]] = future.result()

        for record in records:
            detail = descriptions.get(_description_url(record["url"]))
            record["description"] = detail or record.pop("calendar_description")
            record.pop("calendar_description", None)
            record.pop("event_id", None)
        return records


def main():
    IpoCrawler().run()


if __name__ == "__main__":
    main()
