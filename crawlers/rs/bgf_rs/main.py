import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.bgf.rs/"
SOURCE = "Beogradska filharmonija"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    )
}

MONTHS = {
    "јан": 1, "феб": 2, "мар": 3, "апр": 4, "мај": 5, "јун": 6,
    "јул": 7, "авг": 8, "сеп": 9, "окт": 10, "нов": 11, "дец": 12,
}

# The repertoire is predominantly in Belgrade, but the orchestra also publishes
# touring performances. These markers prevent applying its home city to tours.
LOCATIONS = (
    ("мумбај", "Mumbai", "IN"),
    ("пекинг", "Beijing", "CN"),
    ("сијан", "Xi'an", "CN"),
    ("дортмунд", "Dortmund", "DE"),
    ("марибор", "Maribor", "SI"),
    ("скопљ", "Skopje", "MK"),
    ("тиран", "Tirana", "AL"),
    ("нови сад", "Novi Sad", "RS"),
    ("пожег", "Požega", "RS"),
)


def _clean_text(element):
    if element is None:
        return ""
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def _parse_numeric_date_range(text):
    normalized = text.replace("–", "-").replace("—", "-")
    numbers = [int(value) for value in re.findall(r"\d+", normalized)]
    if len(numbers) < 3:
        return []

    try:
        if "-" in normalized and len(numbers) >= 4:
            # 30.9-3.10.2025 or 20-23.09.2022
            if len(numbers) >= 5:
                start = date(numbers[4], numbers[1], numbers[0])
                end = date(numbers[4], numbers[3], numbers[2])
            else:
                start = date(numbers[3], numbers[2], numbers[0])
                end = date(numbers[3], numbers[2], numbers[1])
            if end < start or (end - start).days > 31:
                return []
            return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]

        return [date(numbers[2], numbers[1], numbers[0])]
    except ValueError:
        return []


def _parse_dates(text):
    dates = _parse_numeric_date_range(text)
    if dates:
        return dates

    lowered = text.lower()
    match = re.search(r"(\d{1,2})\.?\s+([^\W\d_]+)\w*\s+(\d{4})", lowered)
    if not match:
        return []
    month = next((value for stem, value in MONTHS.items() if match.group(2).startswith(stem)), None)
    if month is None:
        return []
    try:
        return [date(int(match.group(3)), month, int(match.group(1)))]
    except ValueError:
        return []


def _parse_times(text):
    normalized = text.replace(".", ":")
    times = re.findall(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", normalized)
    values = [f"{int(hour):02d}:{minute}" for hour, minute in times]

    # The children's listings commonly use forms such as "11 и 12.30".
    first_hour = re.search(r"(?:^|\s)([01]?\d|2[0-3])\s*(?:и|,|/)\s*[0-2]?\d:[0-5]\d", normalized)
    if first_hour:
        values.insert(0, f"{int(first_hour.group(1)):02d}:00")
    return list(dict.fromkeys(values)) or [None]


def _location(venue, page_text):
    evidence = f"{venue} {page_text[:500]}".lower()
    for marker, city, country_code in LOCATIONS:
        if marker in evidence:
            return city, country_code
    return "Belgrade", "RS"


def _fetch_detail(url, listing_title, listing_date):
    log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    section = soup.select_one(".concert-section")
    if section is None:
        return []

    title = _clean_text(section.select_one(".concert-page-title")) or listing_title
    date_text = _clean_text(section.select_one(".date")) or listing_date
    dates = _parse_dates(date_text)
    if not title or not dates:
        return []

    venue = _clean_text(section.select_one(".place"))
    page_text = _clean_text(section)
    if not venue:
        # A venue can be inferred only for entries that show no sign of touring.
        if any(marker in page_text.lower() or marker in title.lower() for marker, _, _ in LOCATIONS):
            return []
        venue = "Сала Београдске филхармоније"

    city, country_code = _location(venue, f"{title} {page_text}")
    times = _parse_times(_clean_text(section.select_one(".time")))
    description = page_text or None
    records = []
    for concert_date in dates:
        for time_from in times:
            records.append({
                "title": title,
                "date": concert_date.isoformat(),
                "url": url,
                "time_from": time_from,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description,
            })
    return records


class BgfCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="bgf_rs",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="RS",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def scrape(self):
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        entries = {}
        for date_element in soup.select(".repertoire-date"):
            card = date_element.find_parent(class_="text-center")
            link = card.select_one("h4 a[href*='/repertoar_cp/']") if card else None
            if link is None:
                continue
            url = link.get("href", "").strip()
            if url:
                entries[url] = (_clean_text(link), _clean_text(date_element))

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(_fetch_detail, url, title, date_text): url
                for url, (title, date_text) in entries.items()
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Concert detail fetch failed",
                        event="crawler_url_fetch_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        log_message("Scrape completed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    BgfCrawler().run()


if __name__ == "__main__":
    main()
