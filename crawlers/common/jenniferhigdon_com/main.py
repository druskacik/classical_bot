import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Jennifer Higdon"
SOURCE_URL = "https://jenniferhigdon.com/"
PERFORMANCES_URL = "https://jenniferhigdon.com/performancesresidencies.html"
HTTP_PERFORMANCES_URL = "http://jenniferhigdon.com/performancesresidencies.html"

US_REGIONS = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming", "D.C", "Puerto Rico",
}

CANADIAN_REGIONS = {
    "Alberta", "British Columbia", "Manitoba", "New Brunswick",
    "Newfoundland and Labrador", "Nova Scotia", "Ontario",
    "Prince Edward Island", "Quebec", "Saskatchewan",
}

COUNTRIES = {
    "Australia": "AU",
    "Austria": "AT",
    "Brazil": "BR",
    "Canada": "CA",
    "England": "GB",
    "Finland": "FI",
    "Ireland": "IE",
    "Mexico": "MX",
    "Norway": "NO",
    "Sweden": "SE",
    "Taiwan": "TW",
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,\n\t")


def _parse_time(value: str) -> str:
    match = re.fullmatch(r"(\d{1,2}):(\d{1,2})(am|pm)", value.lower())
    if not match:
        raise ValueError(f"Unsupported time: {value}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 0 <= minute <= 59 or not 1 <= hour <= 12:
        raise ValueError(f"Invalid time: {value}")
    if match.group(3) == "pm" and hour != 12:
        hour += 12
    elif match.group(3) == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _location(description: str) -> tuple[str, str, str] | None:
    text = description.rstrip(" .")
    region = next(
        (name for name in sorted(US_REGIONS, key=len, reverse=True)
         if re.search(rf",\s*{re.escape(name)}\.?$", text, re.I)),
        None,
    )
    if region:
        country_code = "US"
    else:
        region = next(
            (name for name in sorted(CANADIAN_REGIONS, key=len, reverse=True)
             if re.search(rf",\s*{re.escape(name)}\.?$", text, re.I)),
            None,
        )
        if region:
            country_code = "CA"
        else:
            region = next(
                (name for name in sorted(COUNTRIES, key=len, reverse=True)
                 if re.search(rf",\s*{re.escape(name)}\.?$", text, re.I)),
                None,
            )
            if not region:
                return None
            country_code = COUNTRIES[region]

    before_region = re.sub(rf",\s*{re.escape(region)}\.?$", "", text, flags=re.I)
    held_match = re.search(r"\bheld at\s+(?:the\s+)?(.+?)\s+in\s+([^,]+)$", before_region, re.I)
    if held_match:
        venue = _clean(held_match.group(1))
        city = _clean(held_match.group(2))
        return venue, city, country_code
    if country_code == "MX" and "Mexico City Philharmonic" in description:
        city = "Mexico City"
        before_city = before_region
    elif city_match := re.search(r",\s*([^,]+)$", before_region):
        city = _clean(city_match.group(1))
        before_city = before_region[:city_match.start()]
    else:
        # A few entries use "at VENUE in CITY" rather than comma-separated
        # location text.
        in_city_match = re.search(r"\s+in\s+([^,]+)$", before_region, re.I)
        if in_city_match:
            city = _clean(in_city_match.group(1))
            before_city = before_region[:in_city_match.start()]
        else:
            return None

    venue_matches = list(re.finditer(r"\b(?:at|in)\s+(?:the\s+)?", before_city, re.I))
    if not venue_matches:
        return None
    venue = _clean(before_city[venue_matches[-1].end():])
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def _parse_entry(element) -> list[dict]:
    lines = [line.strip() for line in element.get_text("\n", strip=True).splitlines() if line.strip()]
    if len(lines) < 3:
        return []

    date_match = re.match(
        r"^([A-Z][a-z]+ \d{1,2}, \d{4})(?:,\s*(\d{1,2}:\d{1,2}(?:am|pm)(?:\s+and\s+\d{1,2}:\d{1,2}(?:am|pm))?))?",
        lines[0],
        re.I,
    )
    if not date_match:
        return []
    try:
        event_date = datetime.strptime(date_match.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return []

    italic_values = [_clean(tag.get_text(" ", strip=True)) for tag in element.find_all("i")]
    titles = [value for value in italic_values if "PREMIERE" not in value.upper()]
    if not titles:
        return []
    title = titles[-1]

    description = _clean(element.get_text(" ", strip=True))
    location = _location(description)
    if not location:
        return []
    venue, city, country_code = location

    link = element.find("a", href=True)
    event_url = urljoin(PERFORMANCES_URL, link["href"]) if link else PERFORMANCES_URL
    times = [None]
    if date_match.group(2):
        try:
            times = [_parse_time(value.strip()) for value in re.split(r"\s+and\s+", date_match.group(2), flags=re.I)]
        except ValueError:
            times = [None]

    return [{
        "title": title,
        "date": event_date,
        "url": event_url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    } for time_from in times]


class JenniferHigdonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jenniferhigdon_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city", "country_code"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching performance catalogue", event="crawler_url_fetch", url=PERFORMANCES_URL)
        try:
            response = requests.get(PERFORMANCES_URL, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                "HTTPS fetch failed; retrying canonical page over HTTP",
                event="crawler_url_retry",
                url=PERFORMANCES_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            response = requests.get(HTTP_PERFORMANCES_URL, timeout=30)
            response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")
        records = []
        skipped_count = 0
        for element in soup.select("#main-content div.content-box.table"):
            parsed = _parse_entry(element)
            if parsed:
                records.extend(parsed)
            else:
                skipped_count += 1
        log_message(
            "Parsed performance catalogue",
            event="crawler_parse_completed",
            url=PERFORMANCES_URL,
            record_count=len(records),
            skipped_count=skipped_count,
        )
        return records


def main():
    JenniferHigdonCrawler().run()


if __name__ == "__main__":
    main()
