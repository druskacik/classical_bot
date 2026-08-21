from datetime import date, datetime
import html
import re

from bs4 import BeautifulSoup
import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Gershwin"
SOURCE_URL = "https://gershwin.com/"
EVENTS_URL = "https://gershwin.com/wp-admin/admin-ajax.php"
REQUEST_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://gershwin.com/calendar/",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0; +https://classical.bot)",
    "X-Requested-With": "XMLHttpRequest",
}

US_STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
}

COUNTRY_CODES = {
    "Australia": "AU", "Austria": "AT", "Belgium": "BE", "Brazil": "BR",
    "Canada": "CA", "China": "CN", "Czech Republic": "CZ",
    "Czechia": "CZ", "Denmark": "DK", "England": "GB", "Finland": "FI",
    "France": "FR", "Germany": "DE", "Greece": "GR", "Hungary": "HU",
    "Ireland": "IE", "Israel": "IL", "Italy": "IT", "Japan": "JP",
    "Mexico": "MX", "Monaco": "MC", "Netherlands": "NL",
    "New Zealand": "NZ", "Northern Ireland": "GB", "Norway": "NO",
    "Poland": "PL", "Portugal": "PT", "Scotland": "GB",
    "South Africa": "ZA", "South Korea": "KR", "Spain": "ES",
    "Sweden": "SE", "Switzerland": "CH", "The Netherlands": "NL",
    "United Kingdom": "GB", "United States": "US", "USA": "US",
    "Wales": "GB",
}


def _plain_text(value: str | None) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(html.unescape(value), "html.parser").get_text("\n")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text or None


def _location(value: str | None) -> tuple[str, str] | None:
    parts = [html.unescape(part).strip() for part in (value or "").split(",")]
    parts = [part for part in parts if part]
    if len(parts) < 2 or not parts[0]:
        return None

    country = COUNTRY_CODES.get(parts[-1])
    if country is None and parts[-1] in US_STATES:
        country = "US"
    if country is None:
        return None
    return parts[0], country


def _valid_date(value: str | None) -> str | None:
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


class GershwinCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="gershwin_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["url", "date"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        params = {
            "action": "la__events_list",
            "query_args[posts_per_page]": "-1",
            "query_args[meta_key]": "la__event_start_date",
            "query_args[orderby]": "meta_value",
            "query_args[order]": "ASC",
            "query_args[meta_query][0][key]": "la__event_end_date",
            "query_args[meta_query][0][value]": date.today().isoformat(),
            "query_args[meta_query][0][compare]": ">=",
        }
        log_message("Fetching Gershwin events", event="crawler_url_fetch", url=EVENTS_URL)
        response = requests.get(
            EVENTS_URL,
            params=params,
            headers=REQUEST_HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Gershwin events API returned a non-list response")

        records = []
        for event in payload:
            event_date = _valid_date(event.get("start_date"))
            location = _location(event.get("venue_city_state"))
            title = _plain_text(event.get("title"))
            venue = _plain_text(event.get("venue"))
            url = event.get("permalink")
            if not all((event_date, location, title, venue, url)):
                log_message(
                    "Skipping incomplete Gershwin event",
                    event="crawler_record_skipped",
                    url=url,
                    event_id=event.get("id"),
                )
                continue

            city, country_code = location
            records.append({
                "title": title,
                "date": event_date,
                "url": url,
                "time_from": event.get("start_time") or None,
                "time_to": event.get("end_time") or None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": _plain_text(event.get("content")),
            })

        log_message(
            "Parsed Gershwin events",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    GershwinCrawler().run()


if __name__ == "__main__":
    main()
