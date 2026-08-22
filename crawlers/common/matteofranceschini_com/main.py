import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://matteofranceschini.com/"
CALENDAR_URL = f"{SOURCE_URL}calendar/"
SOURCE = "Matteo Franceschini"

COUNTRY_CODES = {
    "CH": "CH",
    "DE": "DE",
    "FR": "FR",
    "IE": "IE",
    "IT": "IT",
    "NL": "NL",
    "PL": "PL",
    "SE": "SE",
    "SM": "SM",
}

# Some older entries omit the otherwise customary parenthesized country code.
# These cities are stated explicitly in the calendar and make the geography
# unambiguous without treating the composer's home country as a default.
CITY_COUNTRIES = {
    "Avignon": "FR",
    "Falkenberg": "SE",
    "Florence": "IT",
    "Marseille": "FR",
    "Roubaix": "FR",
    "San Marino": "SM",
    "Torino": "IT",
    "Turin": "IT",
    "Vedène": "FR",
    "Venaria Reale": "IT",
}

MONTHS = {
    name: number
    for number, name in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        1,
    )
}


def _clean_text(element) -> str:
    return " ".join(element.stripped_strings)


def _parse_dates(value: str) -> list[str]:
    match = re.fullmatch(
        r"(?:from )?(?P<month>[A-Za-z]+) (?P<first>\d{1,2})"
        r"(?:(?:-| and | to )(?:[A-Za-z]+ )?(?P<last>\d{1,2}))?, (?P<year>\d{4})",
        value.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return []

    month_name = match.group("month").title()
    if month_name not in MONTHS:
        return []
    first = date(int(match.group("year")), MONTHS[month_name], int(match.group("first")))
    last_day = int(match.group("last") or match.group("first"))
    last = date(first.year, first.month, last_day)
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def _geography(venue_text: str) -> tuple[str | None, str | None]:
    code_match = re.search(r"\(([A-Z]{2})\)?", venue_text)
    country_code = COUNTRY_CODES.get(code_match.group(1)) if code_match else None

    normalized = venue_text.casefold()
    city = next(
        (city for city in CITY_COUNTRIES if re.search(rf"\b{re.escape(city.casefold())}\b", normalized)),
        None,
    )
    if city is None and code_match:
        before_code = venue_text[: code_match.start()].strip(" ,–-")
        city = re.split(r"\s+[–-]\s+|,\s*", before_code)[-1].strip()
        city = city.title() if city.isupper() else city

    if city and country_code is None:
        country_code = CITY_COUNTRIES.get(city)
    return city, country_code


def _parse_calendar(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for tab in soup.select(".et_pb_tab"):
        elements = tab.find_all(["h3", "p"], recursive=True)
        starts = [
            index
            for index, element in enumerate(elements)
            if element.name == "p"
            and element.find("strong")
            and re.search(r"\b20\d{2}\b", _clean_text(element))
        ]
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(elements)
            paragraphs = [
                _clean_text(element)
                for element in elements[start:end]
                if element.name == "p" and _clean_text(element)
            ]
            if len(paragraphs) < 3:
                continue

            dates = _parse_dates(paragraphs[0])
            title = paragraphs[1]
            venue = paragraphs[-1]
            city, country_code = _geography(venue)
            if not dates or not title or not venue or not city or not country_code:
                log_message(
                    "Skipping calendar entry with incomplete required fields",
                    event="crawler_record_skipped",
                    url=CALENDAR_URL,
                    date_text=paragraphs[0],
                )
                continue

            description = "\n".join(paragraphs[1:])
            for event_date in dates:
                records.append(
                    {
                        "title": title,
                        "date": event_date,
                        "url": CALENDAR_URL,
                        "time_from": None,
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": description,
                    }
                )
    return records


class MatteoFranceschiniCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="matteofranceschini_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, timeout=30)
        response.raise_for_status()
        records = _parse_calendar(response.text)
        log_message(
            "Calendar parsed",
            event="crawler_scrape_completed",
            url=CALENDAR_URL,
            record_count=len(records),
        )
        return records


def main():
    MatteoFranceschiniCrawler().run()


if __name__ == "__main__":
    main()
