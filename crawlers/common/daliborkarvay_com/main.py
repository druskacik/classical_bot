import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.daliborkarvay.com/"
CONCERTS_URL = "https://www.daliborkarvay.com/concerts"
SOURCE = "Dalibor Karvay"

MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

COUNTRY_CODES = {
    "AUSTRIA": "AT",
    "BELGIUM": "BE",
    "CHINA": "CN",
    "CZECH REPUBLIC": "CZ",
    "GERMANY": "DE",
    "SLOVAKIA": "SK",
    "SOUTH KOREA": "KR",
    "VIETNAM": "VN",
}

INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def clean_text(value: str) -> str:
    value = INVISIBLE_RE.sub("", value).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", value).strip()


def parse_date(value: str) -> str | None:
    """Parse a single calendar date, deliberately rejecting date ranges."""
    value = clean_text(value).upper()
    if re.search(r"\b[A-Z]{3}\s+\d{1,2}\s*-", value) or re.search(
        r"\b\d{1,2}\s*-\s*\d{1,2}\b", value
    ):
        return None

    match = re.search(
        r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})\s+(20\d{2})\b",
        value,
    )
    if not match:
        return None

    try:
        parsed = datetime(
            int(match.group(3)), MONTHS[match.group(1)], int(match.group(2))
        )
    except ValueError:
        return None
    return parsed.date().isoformat()


def parse_times(value: str) -> list[str | None]:
    first_line = clean_text(value.splitlines()[0]) if value.splitlines() else ""
    times = re.findall(r"(?<!\d)([01]?\d|2[0-3])[.:](\d{2})(?!\d)", first_line)
    return [f"{int(hour):02d}:{minute}" for hour, minute in times] or [None]


def parse_location(value: str) -> tuple[str, str] | None:
    parts = [clean_text(part) for part in value.rsplit(",", 1)]
    if len(parts) != 2 or not all(parts):
        return None
    country_code = COUNTRY_CODES.get(parts[1].upper())
    if not country_code:
        return None
    return parts[0].title(), country_code


def extract_venue(detail_block) -> str | None:
    paragraphs = detail_block.find_all("p")
    groups: list[list[str]] = []
    current: list[str] = []
    for paragraph in paragraphs:
        text = clean_text(paragraph.get_text("\n", strip=True))
        if text:
            current.append(text)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    if len(groups) >= 2:
        venue_lines = [
            line for text in groups[-1] for line in text.splitlines() if clean_text(line)
        ]
        return clean_text(venue_lines[0]) if venue_lines else None

    # A few older cards lack the empty paragraph which separates performers
    # from the venue. Accept only lines carrying strong venue-language evidence.
    venue_words = re.compile(
        r"\b(arts cent(?:er|re)|cathedral|church|conservatorium|concert hall|"
        r"festspielhaus|house of music|lechwelten|minoritenzentrum|radio|schloss|"
        r"synagogue|theatre|university)\b",
        re.IGNORECASE,
    )
    lines = [
        clean_text(line)
        for paragraph in paragraphs
        for line in paragraph.get_text("\n", strip=True).splitlines()
        if clean_text(line)
    ]
    candidates = [line for line in lines if venue_words.search(line)]
    return candidates[-1] if candidates else None


def parse_item(item) -> list[dict]:
    blocks = item.find_all("div", class_="wixui-rich-text")
    if len(blocks) < 4:
        return []

    date_block, title_block, location_block, detail_block = blocks[:4]
    date = parse_date(date_block.get_text("\n", strip=True))
    location = parse_location(location_block.get_text(" ", strip=True))
    venue = extract_venue(detail_block)
    if not date or not location or not venue:
        return []

    title_lines = [
        clean_text(line)
        for line in title_block.get_text("\n", strip=True).splitlines()
        if clean_text(line)
    ]
    if not title_lines:
        return []

    city, country_code = location
    link = next(
        (
            anchor
            for anchor in item.find_all("a", href=True)
            if clean_text(anchor.get_text(" ", strip=True)).upper() == "SHOW MORE"
        ),
        None,
    )
    url = link["href"].strip() if link and link["href"].strip() else CONCERTS_URL
    description_parts = title_lines + [
        clean_text(line)
        for line in detail_block.get_text("\n", strip=True).splitlines()
        if clean_text(line)
    ]
    base = {
        "title": " — ".join(title_lines),
        "date": date,
        "url": url,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": "\n".join(description_parts),
    }
    return [{**base, "time_from": time} for time in parse_times(date_block.get_text("\n", strip=True))]


class DaliborKarvayCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="daliborkarvay_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=CONCERTS_URL)
        response = requests.get(CONCERTS_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records: list[dict] = []
        items = soup.select(".wixui-repeater__item")
        for item in items:
            records.extend(parse_item(item))

        log_message(
            "Parsed concert calendar",
            event="crawler_parse_completed",
            url=CONCERTS_URL,
            item_count=len(items),
            record_count=len(records),
        )
        return records


def main():
    DaliborKarvayCrawler().run()


if __name__ == "__main__":
    main()
