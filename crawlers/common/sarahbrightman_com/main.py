from datetime import datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Sarah Brightman"
SOURCE_URL = "https://sarahbrightman.com/"
API_URL = "https://rest.bandsintown.com/artists/Sarah%20Brightman/events"

COUNTRY_CODES = {
    "Argentina": "AR",
    "Australia": "AU",
    "Austria": "AT",
    "Brazil": "BR",
    "Bulgaria": "BG",
    "Canada": "CA",
    "Chile": "CL",
    "China": "CN",
    "Croatia": "HR",
    "Czech Republic": "CZ",
    "Czechia": "CZ",
    "Denmark": "DK",
    "Estonia": "EE",
    "Finland": "FI",
    "Germany": "DE",
    "Greece": "GR",
    "Hungary": "HU",
    "Japan": "JP",
    "Korea, Republic Of": "KR",
    "Latvia": "LV",
    "Lithuania": "LT",
    "Mexico": "MX",
    "Peru": "PE",
    "Poland": "PL",
    "Puerto Rico": "PR",
    "Romania": "RO",
    "Russian Federation": "RU",
    "Saudi Arabia": "SA",
    "Serbia": "RS",
    "Singapore": "SG",
    "Slovakia": "SK",
    "Slovenia": "SI",
    "South Korea": "KR",
    "Spain": "ES",
    "Sweden": "SE",
    "Taiwan": "TW",
    "Turkey": "TR",
    "Ukraine": "UA",
    "United Kingdom": "GB",
    "United States": "US",
    "Uruguay": "UY",
    "日本": "JP",
}

# Bandsintown occasionally stores a production or upsell name where its API
# schema promises a venue. These values are visible in the first-party tour
# listing as event labels, not halls.
NON_VENUE_NAMES = (
    "sunset boulevard",
    "winter symphony",
    "christmas symphony",
    "special offer",
    "meet & greet",
    "schiller -",
)


def clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip()
    return value or None


class SarahBrightmanCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sarahbrightman_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        params = {"app_id": "coya", "date": "all"}
        log_message("Fetching artist events", event="crawler_url_fetch", url=API_URL)
        response = requests.get(API_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Bandsintown artist events response is not a list")

        records = []
        for event in payload:
            record = self._parse_event(event)
            if record is not None:
                records.append(record)

        log_message("Events parsed", event="crawler_records_parsed", record_count=len(records))
        return records

    @staticmethod
    def _parse_event(event: dict) -> dict | None:
        venue_data = event.get("venue") or {}
        event_id = clean_text(event.get("id"))
        url = clean_text(event.get("url"))
        title = clean_text(event.get("title")) or SOURCE
        venue = clean_text(venue_data.get("name"))
        city = clean_text(venue_data.get("city"))
        country_code = COUNTRY_CODES.get(clean_text(venue_data.get("country")))

        # Some malformed records put the tour title in the venue field. The
        # accompanying street address is not a defensible venue name, so these
        # occurrences are skipped rather than emitted with invented locations.
        normalized_venue = venue.casefold() if venue else ""
        invalid_venue = venue in {title, SOURCE} or any(
            marker in normalized_venue for marker in NON_VENUE_NAMES
        )
        if not (event_id and url and venue and city and country_code) or invalid_venue:
            log_message(
                "Skipping event with incomplete location",
                event="crawler_record_skipped",
                url=url,
                event_id=event_id,
            )
            return None

        raw_datetime = clean_text(event.get("datetime"))
        try:
            start = datetime.fromisoformat(raw_datetime) if raw_datetime else None
        except ValueError:
            start = None
        if start is None:
            log_message(
                "Skipping event with invalid date",
                event="crawler_record_skipped",
                url=url,
                event_id=event_id,
            )
            return None

        description = clean_text(event.get("description"))
        lineup = [clean_text(name) for name in event.get("lineup", [])]
        lineup = [name for name in lineup if name]
        if lineup:
            lineup_text = "Performers: " + ", ".join(lineup)
            description = f"{description}\n{lineup_text}" if description else lineup_text

        return {
            "title": title,
            "date": start.date().isoformat(),
            "url": url,
            "time_from": start.strftime("%H:%M"),
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }


def main():
    SarahBrightmanCrawler().run()


if __name__ == "__main__":
    main()
