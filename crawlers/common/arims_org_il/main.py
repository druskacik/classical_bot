from datetime import datetime
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://arims.org.il/"
SOURCE = "Arthur Rubinstein International Music Society"
PAST_EVENTS_URL = f"{SOURCE_URL}past-events/?wpv-wpcf-event-to-date=TODAY%28%29"
FINALS_URL = f"{SOURCE_URL}competition-2026/finals-schedule-2026/"
TIMEOUT = 45
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"}


def _clean(element) -> str:
    if element is None:
        return ""
    text = element.get_text("\n", strip=True).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fetch(session: requests.Session, url: str) -> BeautifulSoup:
    try:
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")
    except requests.RequestException as error:
        log_message(
            "Failed to fetch ARIMS events page",
            event="crawler_fetch_failed",
            level="error",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise


def _location(raw: str) -> tuple[str, str, str] | None:
    """Resolve only locations for which the page supplies defensible geography."""
    value = re.sub(r"\s+", " ", raw).strip(" ,")
    lower = value.lower()
    if lower in {"watsons bay, sydney", "sydney", "tel aviv", "tel-aviv", "israel", "the netherlands"}:
        return None
    rules = [
        (("carnegie hall", "weill recital hall", "weill hall", "zankel hall", "new york", "kosciuszko", "meisel gallery"), "New York", "US"),
        (("wigmore hall", "smith square", "southbank", "london", "private home"), "London", "GB"),
        (("watsons bay", "sydney"), "Sydney", "AU"),
        (("tel aviv", "tel-aviv", "tel aviv university"), "Tel Aviv", "IL"),
        (("jerusalem theatre", "aldwell center"), "Jerusalem", "IL"),
        (("kfar blum",), "Kfar Blum", "IL"),
        (("keshet eilon", "kibbutz eilon", "kibutz eilon"), "Eilon", "IL"),
        (("savyon library",), "Savyon", "IL"),
        (("ashdod performing arts",), "Ashdod", "IL"),
        (("domus galilaeae",), "Korazim", "IL"),
    ]
    for needles, city, country in rules:
        if any(needle in lower for needle in needles):
            if "private residence" in lower or "private home" in lower:
                value = "Private Residence"
            elif "wigmore hall" in lower:
                value = "Wigmore Hall"
            elif "kosciuszko foundation" in lower:
                value = "Kosciuszko Foundation House"
            elif "meisel gallery" in lower:
                value = "Louis K. Meisel Gallery"
            return value, city, country
    return None


def _parse_archive(soup: BeautifulSoup) -> list[dict]:
    loop = soup.select_one(".js-wpv-loop")
    if loop is None:
        return []
    records = []
    for block in loop.select(":scope > .row > .col-sm-12"):
        title_link = block.select_one("h4 a[href]")
        details = block.select_one(".event-details")
        if title_link is None or details is None:
            continue
        values = details.select(".event-data")
        if len(values) < 3:
            continue
        try:
            start = datetime.strptime(_clean(values[0]), "%B %d, %Y %H:%M")
            end = datetime.strptime(_clean(values[1]), "%B %d, %Y %H:%M")
        except ValueError:
            continue
        # Multi-day rows are festival/series overview pages, not one occurrence.
        if start.date() != end.date():
            continue
        location = _location(_clean(values[2]))
        if location is None:
            continue
        venue, city, country_code = location
        description_node = details.find_next_sibling("div")
        records.append({
            "title": _clean(title_link),
            "date": start.date().isoformat(),
            "url": title_link["href"],
            "time_from": start.strftime("%H:%M") if start.time().strftime("%H:%M") != "00:00" else None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": _clean(description_node) or None,
        })
    return records


def _parse_finals(soup: BeautifulSoup) -> list[dict]:
    records = []
    for card in soup.select(".session-card"):
        header = card.select_one(".session-header")
        location_node = card.select_one(".location-info span")
        if header is None or location_node is None:
            continue
        location_text = _clean(location_node).lstrip("📍 ")
        match = re.search(r"(\d{1,2})\.(\d{1,2})\s*\|\s*(\d{2}:\d{2})\s*\|\s*(.+)$", location_text)
        if not match:
            continue
        day, month, time_from, venue = match.groups()
        try:
            event_date = datetime(2026, int(month), int(day)).date().isoformat()
        except ValueError:
            continue
        title_node = header.find("div")
        title = _clean(title_node).split("\n", 1)[0]
        if not title or not venue.strip():
            continue
        records.append({
            "title": title,
            "date": event_date,
            "url": FINALS_URL,
            "time_from": time_from,
            "venue": venue.strip(),
            "city": "Tel Aviv",
            "country_code": "IL",
            "description": _clean(card) or None,
        })
    return records


class ArimsOrgIlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="arims_org_il",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def scrape(self) -> list[dict]:
        with requests.Session() as session:
            session.headers.update(HEADERS)
            records = _parse_finals(_fetch(session, FINALS_URL))
            records.extend(_parse_archive(_fetch(session, PAST_EVENTS_URL)))
        return sorted(records, key=lambda row: (row["date"], row["time_from"] or "", row["title"]))


def main():
    ArimsOrgIlCrawler().run()


if __name__ == "__main__":
    main()
