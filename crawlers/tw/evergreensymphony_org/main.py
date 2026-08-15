import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.evergreensymphony.org/"
SOURCE = "Evergreen Symphony Orchestra"
API_URL = "https://api.cyff.org.tw/api/frontend/eso/concert/list"

# The API does not provide structured venues. These are the halls occurring in
# the orchestra's calendar, including the orchestra's explicitly listed tours.
VENUES = (
    ("NIIGATA CITY PERFORMING ARTS CENTER", "Niigata City Performing Arts Center", "Niigata", "JP"),
    ("TOKYO METROPOLITAN THEATRE", "Tokyo Metropolitan Theatre", "Tokyo", "JP"),
    ("Weiwuying National Kaohsiung Center for the Arts, Concert Hall", "Weiwuying Concert Hall", "Kaohsiung", "TW"),
    ("National Kaohsiung Center for the Arts (Weiwuying)", "Weiwuying Concert Hall", "Kaohsiung", "TW"),
    ("Performance Hall, Cultural Affairs Bureau of Hsinchu County Government", "Performance Hall, Cultural Affairs Bureau of Hsinchu County Government", "Zhubei", "TW"),
    ("Taipei Zhongshan Hall (Zhongzheng Auditorium)", "Taipei Zhongshan Hall (Zhongzheng Auditorium)", "Taipei", "TW"),
    ("Pingtung Performing Arts Center", "Pingtung Performing Arts Center", "Pingtung", "TW"),
    ("Taipei Performing Arts Center", "Taipei Performing Arts Center", "Taipei", "TW"),
    ("Esplanade Concert Hall, Singapore", "Esplanade Concert Hall", "Singapore", "SG"),
    ("National Taichung Theater", "National Taichung Theater", "Taichung", "TW"),
    ("Chang Yung-Fa Foundation B1", "Chang Yung-Fa Foundation B1", "Taipei", "TW"),
    ("Taipei National Concert Hall", "National Concert Hall", "Taipei", "TW"),
    ("National Concert Hall, Taipei", "National Concert Hall", "Taipei", "TW"),
    ("Weiwuying Concert Hall", "Weiwuying Concert Hall", "Kaohsiung", "TW"),
    ("National Concert Hall", "National Concert Hall", "Taipei", "TW"),
)

DATE_TIME_RE = re.compile(
    r"(?P<date>"
    r"\d{4}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}"
    r"|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[a-z.]*\s+\d{4}"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[a-z.]*\s+\d{1,2},?\s+\d{4}"
    r")\s*(?:\([^)]*\))?\s*,?\s*(?:at\s+)?(?P<time>\d{1,2}:\d{2})",
    re.IGNORECASE,
)


def _text_from_item(item: dict) -> str:
    parts = []
    for block in item.get("content") or []:
        if block.get("type") == 1 and block.get("html"):
            parts.append(BeautifulSoup(block["html"], "html.parser").get_text(" ", strip=True))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _parse_date(value: str) -> date:
    value = re.sub(r"\s*([/-])\s*", r"\1", value.strip().replace(".", ""))
    for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported concert date: {value}")


def _venue_after(text: str, position: int):
    segment = text[position:position + 180]
    matches = []
    for needle, venue, city, country_code in VENUES:
        index = segment.casefold().find(needle.casefold())
        if index >= 0:
            matches.append((index, -len(needle), venue, city, country_code))
    return min(matches)[2:] if matches else None


def _fallback_venue(text: str):
    matches = []
    for needle, venue, city, country_code in VENUES:
        index = text.casefold().find(needle.casefold())
        if index >= 0:
            matches.append((index, -len(needle), venue, city, country_code))
    return min(matches)[2:] if matches else None


def _occurrences(item: dict, description: str) -> list[tuple[date, str | None, str, str, str]]:
    # Material after these headings describes talks or ticket collection rather
    # than the concert. Items with no parseable performance line use API dates.
    performance_text = re.split(
        r"PRE[- ]CONCERT TALK|Pre[- ]Concert Talk|Guided Listening|【Ticket Request Information】",
        description,
        maxsplit=1,
    )[0]
    first = date.fromisoformat(item["from_date"])
    last = date.fromisoformat(item.get("end_date") or item["from_date"])
    results = []
    for match in DATE_TIME_RE.finditer(performance_text):
        concert_date = _parse_date(match.group("date"))
        if not first <= concert_date <= last:
            continue
        venue = _venue_after(performance_text, match.end())
        if venue:
            results.append((concert_date, match.group("time"), *venue))

    if results:
        return results

    venue = _fallback_venue(description)
    if venue:
        return [(first, None, *venue)]
    return []


class EvergreenSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="evergreensymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="TW",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def _get_page(self, mode: int, page: int) -> dict:
        params = {
            "lang": 2,
            "mode": mode,
            "page": page,
            "page_size": 100,
            "sort": 0 if mode == 1 else 1,
            "filter": 0,
        }
        log_message("Fetching concert list", event="crawler_url_fetch", url=API_URL, page=page, mode=mode)
        response = requests.get(API_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ValueError("Unexpected concert API response")
        return payload["data"]

    def scrape(self) -> list[dict]:
        items_by_id = {}
        for mode in (1, 2):  # upcoming and the site's complete available archive
            page = 1
            while True:
                data = self._get_page(mode, page)
                items = data.get("items") or []
                for item in items:
                    items_by_id[item["id"]] = item
                if page * 100 >= int(data.get("total") or 0):
                    break
                page += 1

        records = []
        for item in items_by_id.values():
            description = _text_from_item(item)
            url = f"https://www.evergreensymphony.org/en/concert/{item['id']}"
            occurrences = _occurrences(item, description)
            if not occurrences:
                log_message("Skipping concert without a defensible venue", event="crawler_record_skipped", url=url)
                continue
            for concert_date, time_from, venue, city, country_code in occurrences:
                records.append({
                    "title": item["title"].strip(),
                    "date": concert_date.isoformat(),
                    "url": url,
                    "time_from": time_from,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description or None,
                })
        return records


def main():
    EvergreenSymphonyCrawler().run()


if __name__ == "__main__":
    main()
