import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.barisdemirezer.com.tr/"
SOURCE = "Barış Demirezer"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/pages"
CONCERTS_PARENT_ID = 2119

DATE_FORMATS = ("%d %B %Y",)
VENUE_RE = re.compile(r"\bhall\b", re.IGNORECASE)
TIME_RE = re.compile(r",\s*(\d{1,2})[.:](\d{2})(?:\s|$)")
SHORTCODE_RE = re.compile(r"\[/?vc_[^]]*]")
ENSEMBLE_RE = re.compile(
    r"\b(?:orchestra|ensemble|quartet|choir|symphony|philharmonic)\b",
    re.IGNORECASE,
)

KNOWN_VENUE_CITIES = {
    "bilkent concert hall": "Ankara",
    "hacettepe m hall": "Ankara",
    "hacettepe university m hall": "Ankara",
    "presidential symphony orchestra concert hall": "Ankara",
}


def _clean_text(rendered_html: str) -> list[str]:
    soup = BeautifulSoup(rendered_html, "html.parser")
    lines = []
    for raw_line in soup.get_text("\n").splitlines():
        line = SHORTCODE_RE.sub("", unescape(raw_line))
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def _parse_date(title: str) -> str | None:
    title = BeautifulSoup(unescape(title), "html.parser").get_text(" ", strip=True)
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(title, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_venue_and_city(lines: list[str]) -> tuple[str | None, str | None, int | None]:
    for index, line in enumerate(lines):
        if not VENUE_RE.search(line):
            continue

        match = re.match(r"^(.*?),\s*([^,]+?)\s+Turkey\s*$", line, re.IGNORECASE)
        if match:
            return match.group(1).strip(), match.group(2).strip(), index

        venue = line.strip()
        city = KNOWN_VENUE_CITIES.get(venue.casefold())
        return venue, city, index

    return None, None, None


def _event_title(page_title: str, lines: list[str], venue_index: int) -> str:
    for line in lines[:venue_index]:
        if not TIME_RE.search(line) and not line.lower().startswith("concert booklet"):
            return f"{line} — {page_title}"

    for line in lines[venue_index + 1 :]:
        if ENSEMBLE_RE.search(line):
            return f"{line} — {page_title}"

    return page_title


def _parse_page(page: dict) -> dict | None:
    page_title = BeautifulSoup(
        unescape(page["title"]["rendered"]), "html.parser"
    ).get_text(" ", strip=True)
    date = _parse_date(page_title)
    lines = _clean_text(page["content"]["rendered"])
    venue, city, venue_index = _parse_venue_and_city(lines)

    if not date or not venue or not city or venue_index is None:
        log_message(
            "Skipping concert with incomplete required fields",
            event="crawler_record_skipped",
            url=page.get("link"),
            has_date=bool(date),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    time_match = next((TIME_RE.search(line) for line in lines if TIME_RE.search(line)), None)
    time_from = None
    if time_match:
        time_from = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"

    description = "\n".join(lines) or None
    return {
        "title": _event_title(page_title, lines, venue_index),
        "date": date,
        "url": page["link"],
        "time_from": time_from,
        "time_to": None,
        "venue": venue,
        "city": city,
        "description": description,
    }


class BarisDemirezerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="barisdemirezer_com_tr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="TR",
        upload_target="classical",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=3,
                    backoff_factor=1,
                    status_forcelist=(429, 500, 502, 503, 504),
                )
            ),
        )

        pages = []
        page_number = 1
        while True:
            response = session.get(
                API_URL,
                params={
                    "parent": CONCERTS_PARENT_ID,
                    "per_page": 100,
                    "page": page_number,
                    "_fields": "id,link,title,content,parent",
                },
                timeout=30,
            )
            response.raise_for_status()
            pages.extend(response.json())

            total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
            if page_number >= total_pages:
                break
            page_number += 1

        records = []
        for page in pages:
            try:
                record = _parse_page(page)
            except (KeyError, TypeError, ValueError) as error:
                log_message(
                    "Failed to parse concert page",
                    event="crawler_record_parse_failed",
                    url=page.get("link") if isinstance(page, dict) else None,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

        log_message(
            "Concert API parsed",
            event="crawler_api_parsed",
            url=response.url,
            record_count=len(records),
        )
        return records


def main():
    BarisDemirezerCrawler().run()


if __name__ == "__main__":
    main()
