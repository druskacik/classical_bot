from datetime import datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Julia Holter"
SOURCE_URL = "https://juliaholter.com/"
EVENTS_URL = (
    "https://rest.bandsintown.com/V3.1/artists/Julia%20Holter/events/"
    "?app_id=js_juliaholter.com&date=all"
)

COUNTRY_CODES = {
    "ARGENTINA": "AR",
    "AUSTRALIA": "AU",
    "AUSTRIA": "AT",
    "BELGIUM": "BE",
    "BRAZIL": "BR",
    "CANADA": "CA",
    "CHILE": "CL",
    "CHINA": "CN",
    "CZECH REPUBLIC": "CZ",
    "DENMARK": "DK",
    "FINLAND": "FI",
    "FRANCE": "FR",
    "GERMANY": "DE",
    "HONG KONG": "HK",
    "ICELAND": "IS",
    "IRELAND": "IE",
    "ITALY": "IT",
    "JAPAN": "JP",
    "KOREA, REPUBLIC OF": "KR",
    "LATVIA": "LV",
    "LUXEMBOURG": "LU",
    "MEXICO": "MX",
    "NETHERLANDS": "NL",
    "NEW ZEALAND": "NZ",
    "NORWAY": "NO",
    "PERU": "PE",
    "POLAND": "PL",
    "PORTUGAL": "PT",
    "SINGAPORE": "SG",
    "SPAIN": "ES",
    "SWEDEN": "SE",
    "SWITZERLAND": "CH",
    "UNITED KINGDOM": "GB",
    "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND": "GB",
    "UNITED STATES": "US",
    "URUGUAY": "UY",
}


def _country_code(country: str | None) -> str | None:
    if not country:
        return None
    return COUNTRY_CODES.get(country.strip().upper())


def _description(event: dict) -> str | None:
    parts = []
    description = (event.get("description") or "").strip()
    if description:
        parts.append(description)

    lineup = [name.strip() for name in event.get("lineup", []) if name.strip()]
    if lineup:
        parts.append(f"Lineup: {', '.join(lineup)}")

    return "\n\n".join(parts) or None


class JuliaHolterCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="juliaholter_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="potential",
        dedupe_subset=["date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching artist events", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(EVENTS_URL, timeout=30)
        response.raise_for_status()
        events = response.json()

        records = []
        for event in events:
            venue_data = event.get("venue") or {}
            venue = (venue_data.get("name") or "").strip()
            city = (venue_data.get("city") or "").strip()
            country_code = _country_code(venue_data.get("country"))
            event_url = (event.get("url") or "").strip()

            if not venue or not city or not country_code or not event_url:
                log_message(
                    "Skipping event with incomplete location",
                    event="crawler_record_skipped",
                    url=event_url or SOURCE_URL,
                    event_id=event.get("id"),
                )
                continue

            try:
                event_datetime = datetime.fromisoformat(event["datetime"])
            except (KeyError, TypeError, ValueError) as error:
                log_message(
                    "Skipping event with invalid date",
                    event="crawler_record_skipped",
                    url=event_url,
                    event_id=event.get("id"),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            title = (event.get("title") or "").strip() or f"Julia Holter at {venue}"
            time_from = None
            if event.get("datetime_display_rule") != "date":
                time_from = event_datetime.strftime("%H:%M")

            records.append(
                {
                    "title": title,
                    "date": event_datetime.date().isoformat(),
                    "url": event_url,
                    "time_from": time_from,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": _description(event),
                }
            )

        return records


def main():
    JuliaHolterCrawler().run()


if __name__ == "__main__":
    main()
