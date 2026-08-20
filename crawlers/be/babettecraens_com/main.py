from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Babette Craens"
SOURCE_URL = "https://www.babettecraens.com/"
AGENDA_URL = f"{SOURCE_URL}agenda"
COUNTRY_CODES = {
    "Belgium": "BE",
    "Netherlands": "NL",
}
COUNTRY_TIMEZONES = {
    "BE": ZoneInfo("Europe/Brussels"),
    "NL": ZoneInfo("Europe/Amsterdam"),
}


def _text(html):
    if not html:
        return None
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text or None


def _city(location):
    address_line = location.get("addressLine2", "")
    return address_line.split(",", 1)[0].strip() or None


def _is_concrete_public_performance(item):
    evidence = " ".join(
        filter(None, (item.get("title"), _text(item.get("excerpt"))))
    ).casefold()
    return "private event" not in evidence and "recording" not in evidence and "opname" not in evidence


class BabetteCraensCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="babettecraens_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="BE",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching agenda", event="crawler_url_fetch", url=AGENDA_URL)
        response = requests.get(
            AGENDA_URL,
            params={"format": "json"},
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()

        records = []
        seen_ids = set()
        for section in ("upcoming", "past"):
            for item in payload.get(section, []):
                item_id = item.get("id")
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                location = item.get("location") or {}
                country_code = COUNTRY_CODES.get(location.get("addressCountry"))
                city = _city(location)
                venue = (location.get("addressTitle") or "").strip() or None
                if not _is_concrete_public_performance(item) or not all(
                    (item.get("title"), item.get("fullUrl"), item.get("startDate"), venue, city, country_code)
                ):
                    continue

                timezone = COUNTRY_TIMEZONES[country_code]
                start = datetime.fromtimestamp(item["startDate"] / 1000, timezone)
                end_timestamp = item.get("endDate")
                end = datetime.fromtimestamp(end_timestamp / 1000, timezone) if end_timestamp else None
                description_parts = [_text(item.get("excerpt")), _text(item.get("body"))]
                description = "\n\n".join(part for part in description_parts if part) or None

                records.append(
                    {
                        "title": " ".join(item["title"].split()),
                        "date": start.date().isoformat(),
                        "url": requests.compat.urljoin(SOURCE_URL, item["fullUrl"]),
                        "time_from": start.time().replace(tzinfo=None).isoformat(timespec="minutes"),
                        "time_to": end.time().replace(tzinfo=None).isoformat(timespec="minutes") if end else None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": description,
                    }
                )

        log_message(
            "Agenda parsed",
            event="crawler_scrape_completed",
            url=AGENDA_URL,
            record_count=len(records),
        )
        return records


def main():
    BabetteCraensCrawler().run()


if __name__ == "__main__":
    main()
