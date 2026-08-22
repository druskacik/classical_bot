import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://mikyungbass.com/"
SOURCE = "Mikyung Sung"
ARCHIVE_PATHS = ("concerts", "concerts/past", "concerts/colburn", "concerts/early")

COUNTRY_CODES = {
    "austria": "AT",
    "china": "CN",
    "germany": "DE",
    "korea": "KR",
    "mexico": "MX",
    "norway": "NO",
    "singapore": "SG",
    "south korea": "KR",
    "uk": "GB",
    "usa": "US",
}
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

# Some entries omit a country or put the city inside the venue name. These
# first-party location strings are stable enough to resolve explicitly.
LOCATION_HINTS = {
    "dendrinos chapel": ("Interlochen", "US"),
    "drew school": ("San Francisco", "US"),
    "gunsan arts center": ("Gunsan", "KR"),
    "last stradeum": ("Seoul", "KR"),
    "mcm cheongdam": ("Seoul", "KR"),
    "naver v live": (None, None),
    "nikolaisaal potsdam": ("Potsdam", "DE"),
    "pohang culture": ("Pohang", "KR"),
    "private home in yangpyeong": ("Yangpyeong", "KR"),
    "shanghai symphony": ("Shanghai", "CN"),
}


def _clean_text(element) -> str:
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def _location_parts(item) -> tuple[str, str] | None:
    field = item.select_one(".field--name-field-venue")
    if field is None:
        return None

    label = field.select_one(".field__label")
    if label is not None:
        label.extract()
    location = _clean_text(field)
    if not location:
        return None

    strong = field.find("strong")
    venue = _clean_text(strong) if strong is not None else location.split(",", 1)[0].strip()
    return venue, location


def _geography(location: str) -> tuple[str, str] | None:
    lowered = location.lower()
    for marker, result in LOCATION_HINTS.items():
        if marker in lowered:
            return result if all(result) else None

    parts = [part.strip() for part in location.split(",") if part.strip()]
    if not parts:
        return None

    country_code = COUNTRY_CODES.get(parts[-1].lower())
    end = len(parts) - 1 if country_code else len(parts)

    state_index = None
    for index in range(end - 1, -1, -1):
        if parts[index].upper() in US_STATES:
            state_index = index
            country_code = "US"
            break

    if country_code == "SG":
        return "Singapore", country_code

    if state_index is not None:
        if state_index >= 1:
            city = parts[state_index - 1]
        elif "kennesaw state university" in lowered:
            city = "Kennesaw"
        else:
            return None
        return city, country_code

    if country_code is None:
        return None

    region_markers = {"gyeonggi", "shandong"}
    city_index = end - 1
    if city_index >= 1 and parts[city_index].lower() in region_markers:
        city_index -= 1

    if city_index >= 1:
        return parts[city_index], country_code

    # A few European venue strings include their city without a comma.
    if "potsdam" in lowered:
        return "Potsdam", country_code
    if "frankfurt (oder)" in lowered:
        return "Frankfurt (Oder)", country_code
    return None


def _parse_date_and_time(item) -> tuple[str, str | None] | None:
    field = item.select_one(".field--name-field-date")
    if field is None:
        return None

    time_element = field.select_one("time[datetime]")
    if time_element is not None:
        date_value = time_element["datetime"][:10]
    else:
        value = field.select_one(".field__item")
        if value is None:
            return None
        try:
            date_value = datetime.strptime(_clean_text(value), "%A, %B %d, %Y").date().isoformat()
        except ValueError:
            return None

    start = field.select_one(".start-time")
    if start is None:
        return date_value, None
    match = re.search(r"(\d{1,2}:\d{2})\s*([ap]m)", _clean_text(start), re.I)
    if match is None:
        return date_value, None
    parsed_time = datetime.strptime("".join(match.groups()), "%I:%M%p").time()
    return date_value, parsed_time.strftime("%H:%M:%S")


def _parse_archive(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".view-future-concerts > .view-content")
    if content is None:
        raise ValueError(f"Concert listing not found at {page_url}")

    records = []
    for item in content.find_all(recursive=False):
        if item.name == "h3":
            continue

        title_element = item.select_one(".field--name-node-title h3, h4")
        parsed_datetime = _parse_date_and_time(item)
        parsed_location = _location_parts(item)
        if title_element is None or parsed_datetime is None or parsed_location is None:
            continue

        venue, location = parsed_location
        geography = _geography(location)
        if not venue or geography is None:
            continue
        city, country_code = geography
        date_value, time_from = parsed_datetime

        records.append({
            "title": _clean_text(title_element),
            "date": date_value,
            "url": page_url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": _clean_text(item),
        })
    return records


class MikyungBassCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="mikyungbass_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["title", "date", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        with requests.Session() as session:
            for path in ARCHIVE_PATHS:
                url = f"{SOURCE_URL}{path}"
                log_message("Fetching concert archive", event="crawler_url_fetch", url=url)
                try:
                    response = session.get(url, timeout=30)
                    response.raise_for_status()
                    records.extend(_parse_archive(response.text, url))
                except requests.RequestException as error:
                    log_message(
                        "Concert archive fetch failed",
                        event="crawler_url_fetch_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    raise

        log_message("Concert archives parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    MikyungBassCrawler().run()


if __name__ == "__main__":
    main()
