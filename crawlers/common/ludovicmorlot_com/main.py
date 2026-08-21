import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Ludovic Morlot"
SOURCE_URL = "https://ludovicmorlot.com/"
SCHEDULE_URL = "https://ludovicmorlot.com/schedule/"

COUNTRY_CODES = {
    "Canada": "CA",
    "Denmark": "DK",
    "Germany": "DE",
    "Italy": "IT",
    "Japan": "JP",
    "Netherlands": "NL",
    "Norway": "NO",
    "South Korea": "KR",
    "Spain": "ES",
    "Sweden": "SE",
    "United Kingdom": "GB",
    "United States": "US",
    "USA": "US",
    "US": "US",
}

# Some cards omit the city as a separate comma-delimited location component.
# These venue strings are stable first-party labels from the schedule itself.
CITY_BY_VENUE = {
    "Aalborg Kongres & Kultur Center": "Aalborg",
    "Blackpool Opera House": "Blackpool",
    "Ely Cathedral": "Ely",
    "Kölner Philharmonie": "Cologne",
    "L’Auditori Barcelona": "Barcelona",
    "Theater aan de Parade Den Bosch": "Den Bosch",
    "Trondheim Symphony Orchestra": "Trondheim",
}

DATE_TIME_RE = re.compile(
    r"^(?P<title>.+?)\s+[–-]\s+"
    r"(?P<date>(?:[A-Za-z]+\s+\d{1,2}|\d{1,2}\s+[A-Za-z]+),\s+\d{4}),\s+"
    r"(?P<time>\d{1,2}[.:]\d{2}\s*(?:am|pm))$",
    re.IGNORECASE,
)


def _parse_title_date_time(text: str) -> tuple[str, str, str]:
    match = DATE_TIME_RE.match(" ".join(text.split()))
    if not match:
        raise ValueError(f"Unrecognized schedule heading: {text!r}")

    date_text = match.group("date")
    parsed_date = None
    for date_format in ("%B %d, %Y", "%d %B, %Y"):
        try:
            parsed_date = datetime.strptime(date_text, date_format).date()
            break
        except ValueError:
            continue
    if parsed_date is None:
        raise ValueError(f"Unrecognized concert date: {date_text!r}")

    time_text = match.group("time").lower().replace(".", ":").replace(" ", "")
    hour, minute = (int(part) for part in time_text[:-2].split(":"))
    meridiem = time_text[-2:]
    if hour <= 12:
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid concert time: {match.group('time')!r}")

    return match.group("title").strip(), parsed_date.isoformat(), f"{hour:02d}:{minute:02d}"


def _parse_location(text: str) -> tuple[str, str, str]:
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) < 2:
        raise ValueError(f"Incomplete concert location: {text!r}")

    country_name = parts[-1]
    country_code = COUNTRY_CODES.get(country_name)
    if country_code is None:
        raise ValueError(f"Unknown concert country: {country_name!r}")

    venue = parts[0]
    city = CITY_BY_VENUE.get(venue)
    if city is None:
        # Location cards are formatted venue, city, [state/province,] country.
        city = parts[1] if len(parts) >= 3 else None
    if not city:
        raise ValueError(f"Concert city is not available: {text!r}")
    return venue, city, country_code


def _custom_fields(article) -> list[str]:
    return [
        field.get_text(" ", strip=True)
        for field in article.select(".elementor-post-info__item--type-custom")
        if field.get_text(" ", strip=True)
    ]


class LudovicMorlotCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="ludovicmorlot_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message(
            "Fetching schedule",
            event="crawler_url_fetch",
            level="info",
            url=SCHEDULE_URL,
        )
        response = requests.get(SCHEDULE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for article in soup.select("article.schedule"):
            try:
                heading = article.select_one("h4")
                fields = _custom_fields(article)
                booking_link = article.select_one("a[href]")
                if heading is None or booking_link is None or len(fields) < 3:
                    raise ValueError("Schedule card is missing required fields")

                title, concert_date, time_from = _parse_title_date_time(
                    heading.get_text(" ", strip=True)
                )
                venue, city, country_code = _parse_location(fields[0])
                description = fields[2] if fields[2] not in {"–", "-"} else None
                url = booking_link.get("href", "").strip()
                if not url:
                    raise ValueError("Schedule card has an empty booking URL")

                records.append(
                    {
                        "title": title,
                        "date": concert_date,
                        "url": url,
                        "time_from": time_from,
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": description,
                    }
                )
            except (AttributeError, TypeError, ValueError) as error:
                log_message(
                    "Skipping invalid schedule card",
                    event="crawler_record_skipped",
                    level="warning",
                    post_id=article.get("id"),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        log_message(
            "Schedule parsed",
            event="crawler_scrape_completed",
            level="info",
            record_count=len(records),
        )
        return records


def main():
    LudovicMorlotCrawler().run()


if __name__ == "__main__":
    main()
