import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "M. K. Čiurlionis 150"
SOURCE_URL = "https://ciurlionis.eu/"
API_URL = (
    "https://ciurlionis.eu/api/public/features/"
    "72e02345-7fa9-48cd-9088-b8fe70b504bc/items/detailed"
)

COUNTRY_CODES = {
    "add-latvia": "LV",
    "country-Netherlands": "NL",
    "country-Norvegija": "NO",
    "country-austria": "AT",
    "country-azerbaijan": "AZ",
    "country-brasil": "BR",
    "country-bulgaria": "BG",
    "country-canada": "CA",
    "country-czechia": "CZ",
    "country-denmark": "DK",
    "country-estonia": "EE",
    "country-finland": "FI",
    "country-france": "FR",
    "country-georgia": "GE",
    "country-germany": "DE",
    "country-graikija": "GR",
    "country-india": "IN",
    "country-italija": "IT",
    "country-japan": "JP",
    "country-lithuania": "LT",
    "country-moldova": "MD",
    "country-poland": "PL",
    "country-slovenia": "SI",
    "country-south-korea": "KR",
    "country-spain": "ES",
    "country-sweden": "SE",
    "country-taiwan": "TW",
    "country-turkey": "TR",
    "country-united-kingdom": "GB",
    "country-united-states-of-america": "US",
}


def _attribute(item, slug):
    return next((value for value in item.get("attributes", []) if value.get("slug") == slug), None)


def _localized_value(attribute):
    if not attribute:
        return None
    values = attribute.get("values") or []
    for language in ("en", "lt"):
        value = next(
            (entry.get("value") for entry in values if entry.get("languageCode") == language),
            None,
        )
        if value:
            return value
    return next((entry.get("value") for entry in values if entry.get("value")), None)


def _tag(attribute):
    value = _localized_value(attribute)
    return value if isinstance(value, str) else None


def _city(item):
    tag = _tag(_attribute(item, "event-city"))
    if not tag or tag == "event":
        return None
    city = re.sub(r"^(?:city|event|ebent)-", "", tag)
    return city.replace("-", " ").strip().title() or None


def _description(item):
    raw = _localized_value(_attribute(item, "description"))
    if not raw:
        return None
    text = BeautifulSoup(unescape(raw), "html.parser").get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def _parse_item(item):
    title = _localized_value(_attribute(item, "title"))
    date_attribute = _attribute(item, "date")
    date_config = (date_attribute or {}).get("configuration") or {}
    event_date = date_config.get("isoDate")
    location = _attribute(item, "location")
    location_config = (location or {}).get("configuration") or {}
    venue = location_config.get("displayAddress")
    city = _city(item)
    country_code = COUNTRY_CODES.get(_tag(_attribute(item, "tag-country")))
    slug = item.get("slug")

    try:
        date.fromisoformat(event_date)
    except (TypeError, ValueError):
        return None
    if not all((title, slug, venue, city, country_code)):
        return None
    if venue.casefold() == city.casefold():
        return None

    time_from = date_config.get("displayTime") or None
    if time_from and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_from):
        time_from = None

    return {
        "title": title.strip(),
        "date": event_date,
        "url": f"https://ciurlionis.eu/en/content/{slug}?source=events",
        "time_from": time_from,
        "venue": venue.strip(),
        "city": city,
        "country_code": country_code,
        "description": _description(item),
    }


class CiurlionisCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="ciurlionis_eu",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        session = requests.Session()
        page = 1
        records = []
        skipped = 0

        while True:
            log_message("Fetching events page", event="crawler_url_fetch", url=API_URL, page=page)
            response = session.get(
                API_URL,
                params={
                    "page": page,
                    "perPage": 100,
                    "attributeSorting[0][attributeSlug]": "startDate",
                    "attributeSorting[0][order]": "asc",
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("data") or []
            for item in items:
                record = _parse_item(item)
                if record:
                    records.append(record)
                else:
                    skipped += 1

            last_page = payload.get("lastPage") or 0
            if page >= last_page:
                break
            page += 1

        log_message(
            "Events parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
            skipped_count=skipped,
        )
        return records


def main():
    CiurlionisCrawler().run()


if __name__ == "__main__":
    main()
