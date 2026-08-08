import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Kapellmeister"
SOURCE_URL = "https://www.kapellmeister.info/"
CONCERTS_URL = "https://www.kapellmeister.info/concerten.html"

MONTHS = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}

DATE_RE = re.compile(
    r"(?:maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag)\s*,?\s*"
    r"(?P<day>\d{1,2})\s+(?P<month>" + "|".join(MONTHS) + r")\s+"
    r"(?P<year>20\d{2})"
    r"(?:\s*,?\s*(?P<hour>\d{1,2})(?::(?P<minute_colon>\d{2})|u(?P<minute_u>\d{2})?))?\s*:?,?",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r"^\s*(?P<city>['’]?[A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ .'-]*?(?:\s*\(NL\))?)\s*,\s*(?P<venue>.*)$"
)


def _clean(value):
    value = value.replace("\u200b", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip(" \t\r\n,|")


def _parse_page(html):
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".wsite-section-elements")
    if content is None:
        raise ValueError("Concert listing container was not found")

    # Older archive entries are grouped into a single Weebly paragraph, so
    # split every paragraph again whenever another Dutch date heading begins.
    text = "\n".join(content.stripped_strings)
    text = text.replace("Z\naterdag", "Zaterdag")
    matches = list(DATE_RE.finditer(text))
    records = []

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():end]
        if re.search(r"\b(?:geannuleerd|uitgesteld)\b", block, re.IGNORECASE):
            continue

        lines = [_clean(line) for line in block.splitlines()]
        lines = [line for line in lines if line and line not in {"-", "$$r"}]

        location_index = None
        location_match = None
        for line_index, line in enumerate(lines):
            candidate = LOCATION_RE.match(line)
            if candidate:
                location_index = line_index
                location_match = candidate

        if location_match is None:
            continue

        city_raw = _clean(location_match.group("city"))
        country_code = "NL" if "(NL)" in city_raw.upper() else "BE"
        city = re.sub(r"\s*\(NL\)\s*", "", city_raw, flags=re.IGNORECASE).strip()
        venue = _clean(location_match.group("venue"))
        if (not venue or venue.lower() == "matineevoorstelling") and location_index + 1 < len(lines):
            venue = _clean(lines[location_index + 1])
        venue = re.sub(r"^Kapellmeister\s+a\s+\d+\s*,\s*", "", venue, flags=re.IGNORECASE)
        if (
            venue.upper() == "NL"
            or not venue
            or re.match(r"^(?:stichting\s|klassiek in de kerken\b|bach in de stad\b)", venue, re.IGNORECASE)
        ):
            continue

        detail_lines = lines[:location_index]
        detail_lines = [line for line in detail_lines if line.lower() not in {"tickets", "ticket", "gratis"}]
        if not detail_lines:
            continue
        title = detail_lines[0]
        if re.match(r"^a\s+\d+\b", title, re.IGNORECASE) and len(detail_lines) > 1:
            title = detail_lines[1]
        title = _clean(title)
        if not title:
            continue

        event_date = date(
            int(match.group("year")),
            MONTHS[match.group("month").lower()],
            int(match.group("day")),
        ).isoformat()
        time_from = None
        if match.group("hour") is not None:
            minute = match.group("minute_colon") or match.group("minute_u") or "00"
            time_from = f'{int(match.group("hour")):02d}:{minute}'

        description = "\n".join(detail_lines) or None
        records.append(
            {
                "title": title,
                "date": event_date,
                "url": CONCERTS_URL,
                "time_from": time_from,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description,
            }
        )

    return records


class KapellmeisterInfoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="kapellmeister_info",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="BE",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching concert listing", event="crawler_url_fetch", url=CONCERTS_URL)
        try:
            response = requests.get(
                CONCERTS_URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
                timeout=30,
            )
            response.raise_for_status()
            records = _parse_page(response.text)
        except Exception as error:
            log_message(
                "Concert listing fetch or parse failed",
                event="crawler_fetch_failed",
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        log_message(
            "Concert listing parsed",
            event="crawler_scrape_completed",
            url=CONCERTS_URL,
            record_count=len(records),
        )
        return records


def main():
    KapellmeisterInfoCrawler().run()


if __name__ == "__main__":
    main()
