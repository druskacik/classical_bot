from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
import re
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Magyar Rádió Művészeti Együttesei"
SOURCE_URL = "https://radiomusic.hu/koncertnaptar/"
FEED_URL = (
    "https://radiomusic.hu/wp-content/plugins/mtva-corp-plugin/"
    "interfaces/data_feed.php"
)
CATEGORIES = ("255", "267", "273", "261")
FIRST_ARCHIVE_YEAR = 2016
REQUEST_TIMEOUT = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": SOURCE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

# The feed has a venue, but no separate locality. These rules cover the
# institution's regular halls and explicitly named touring cities. Unknown
# locations are deliberately skipped rather than assigned the home city.
LOCATION_RULES = (
    ("müpa", "Budapest", "HU"),
    ("mupa", "Budapest", "HU"),
    ("zeneakadémia", "Budapest", "HU"),
    ("zeneakademia", "Budapest", "HU"),
    ("dohnányi ernő zenei központ", "Budapest", "HU"),
    ("dohnanyi erno zenei kozpont", "Budapest", "HU"),
    ("mátyás-templom", "Budapest", "HU"),
    ("matyas-templom", "Budapest", "HU"),
    ("pesti vigadó", "Budapest", "HU"),
    ("pesti vigado", "Budapest", "HU"),
    ("budapest music center", "Budapest", "HU"),
    ("bmc", "Budapest", "HU"),
    ("magyar nemzeti múzeum", "Budapest", "HU"),
    ("magyar nemzeti muzeum", "Budapest", "HU"),
    ("budapest", "Budapest", "HU"),
    ("debrecen", "Debrecen", "HU"),
    ("pécs", "Pécs", "HU"),
    ("pecs", "Pécs", "HU"),
    ("szeged", "Szeged", "HU"),
    ("győr", "Győr", "HU"),
    ("gyor", "Győr", "HU"),
    ("miskolc", "Miskolc", "HU"),
    ("veszprém", "Veszprém", "HU"),
    ("veszprem", "Veszprém", "HU"),
    ("kecskemét", "Kecskemét", "HU"),
    ("kecskemet", "Kecskemét", "HU"),
    ("szombathely", "Szombathely", "HU"),
    ("sopron", "Sopron", "HU"),
    ("esztergom", "Esztergom", "HU"),
)


def canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def location_for(venue: str) -> tuple[str, str] | None:
    folded = venue.casefold()
    for needle, city, country_code in LOCATION_RULES:
        if needle in folded:
            return city, country_code
    return None


def fetch_month(session: requests.Session, year: int, month: int) -> list[dict]:
    payload = [("date", f"{year}-{month}")]
    payload.extend(("categories[]", category) for category in CATEGORIES)
    payload.append(("widget", "mrze_concertcalendarwidget-7"))
    response = session.post(
        FEED_URL, data=payload, headers=HEADERS, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, list):
        raise ValueError("Calendar feed did not return a list")
    return result


def fetch_description(url: str) -> str | None:
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        content = soup.select_one(".hms_article_post_content")
        if content is None:
            return None
        text = content.get_text("\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", text) or None
    except (requests.RequestException, ValueError) as error:
        log_message(
            "Could not fetch concert detail",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


class RadioMusicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="radiomusic_hu",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="HU",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        # Establish the same session/cookies as the calendar page before using
        # its first-party XHR endpoint.
        response = session.get(SOURCE_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        raw_events: list[dict] = []
        current_year = date.today().year

        # The public calendar's year selector begins at 2016. Query every
        # archived month, then continue through the next calendar year so that
        # early-announced future performances are retained as well.
        for year in range(FIRST_ARCHIVE_YEAR, current_year + 2):
            for month in range(1, 13):
                try:
                    raw_events.extend(fetch_month(session, year, month))
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        "Could not fetch calendar month",
                        event="crawler_url_fetch_failed",
                        url=FEED_URL,
                        year=year,
                        month=month,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records: list[dict] = []
        seen: set[tuple[str, str, str, str]] = set()
        for event in raw_events:
            title = str(event.get("title") or "").strip()
            venue = str(event.get("event_place") or "").strip()
            permalink = str(event.get("permalink") or "").strip()
            event_time = str(event.get("event_time") or "").strip()
            location = location_for(venue)
            if not (title and venue and permalink and event_time and location):
                continue
            try:
                starts_at = datetime.strptime(event_time, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            url = canonical_url(permalink)
            key = (url, starts_at.date().isoformat(), starts_at.time().isoformat(), venue)
            if key in seen:
                continue
            seen.add(key)
            city, country_code = location
            records.append(
                {
                    "title": title,
                    "date": starts_at.date().isoformat(),
                    "url": url,
                    "time_from": starts_at.strftime("%H:%M"),
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": str(event.get("excerpt") or "").strip() or None,
                }
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_record = {
                executor.submit(fetch_description, record["url"]): record
                for record in records
            }
            for future in as_completed(future_to_record):
                description = future.result()
                if description:
                    future_to_record[future]["description"] = description

        log_message(
            "Calendar records parsed",
            event="crawler_records_parsed",
            record_count=len(records),
            skipped_count=len(raw_events) - len(records),
        )
        return records


def main():
    RadioMusicCrawler().run()


if __name__ == "__main__":
    main()
