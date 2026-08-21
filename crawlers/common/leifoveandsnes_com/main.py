import html
import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://leifoveandsnes.com/"
SOURCE = "Leif Ove Andsnes"
CALENDAR_URL = "https://leifoveandsnes.com/concerts/"
AJAX_URL = "https://leifoveandsnes.com/wp-admin/admin-ajax.php"

COUNTRIES = {
    "Austria": "AT",
    "Belgium": "BE",
    "Brazil": "BR",
    "Canada": "CA",
    "Czechia": "CZ",
    "Denmark": "DK",
    "Estonia": "EE",
    "France": "FR",
    "Germany": "DE",
    "Hungary": "HU",
    "Italy": "IT",
    "Japan": "JP",
    "Lithuania": "LT",
    "Netherlands": "NL",
    "Norway": "NO",
    "Portugal": "PT",
    "Romania": "RO",
    "Spain": "ES",
    "Sweden": "SE",
    "Switzerland": "CH",
    "Taiwan": "TW",
    "UK": "GB",
    "USA": "US",
}


def _clean(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip() or None


def _geography(title):
    """Read the city and country from the schedule's consistently geographic title."""
    clean_title = _clean(title)
    segments = [part.strip() for part in re.split(r"\s+[–—-]\s+", clean_title)]

    for segment in segments:
        for country, code in COUNTRIES.items():
            match = re.search(rf"\b{re.escape(country)}\b", segment, re.IGNORECASE)
            if not match:
                continue
            city = segment[:match.start()].rstrip(" ,")
            # US titles include a state abbreviation between city and country.
            city = re.sub(r",\s*[A-Z]{2}$", "", city).strip()
            if city:
                return city, code

    return None, None


def _time_24h(value):
    value = _clean(value)
    if not value:
        return None
    try:
        return datetime.strptime(value.upper(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        log_message(
            "Skipping unparseable event time",
            event="crawler_parse_warning",
            value=value,
        )
        return None


class LeifOveAndsnesCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="leifoveandsnes_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def _page(self, session, start_date, offset):
        payload = {
            "action": "mec_list_load_more",
            "mec_start_date": start_date,
            "mec_offset": str(offset),
            "atts[skin]": "full_calendar",
            "atts[sk-options][full_calendar][default_view]": "list",
            "atts[sk-options][list][limit]": "100",
            "atts[sk-options][list][order_method]": "ASC",
            "atts[sk-options][list][style]": "standard",
            "atts[sk-options][list][from_fc]": "1",
            "atts[show_past_events]": "1",
            "atts[show_only_past_events]": "0",
            "atts[id]": "4405",
        }
        log_message(
            "Fetching concert calendar page",
            event="crawler_url_fetch",
            url=AJAX_URL,
            start_date=start_date,
            offset=offset,
        )
        response = session.post(AJAX_URL, data=payload, timeout=60)
        response.raise_for_status()
        return response.json()

    def scrape(self):
        session = requests.Session()
        session.headers.update(
            {
                "Referer": CALENDAR_URL,
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
            }
        )

        records = []
        # MEC rejects very old start years. This predates the site's first
        # published calendar event (April 2025) while retaining archives.
        start_date = "2024-01-01"
        offset = 0
        while True:
            result = self._page(session, start_date, offset)
            soup = BeautifulSoup(result.get("html", ""), "html.parser")
            scripts = soup.select('script[type="application/ld+json"]')

            for script in scripts:
                try:
                    event = json.loads(script.string or "")
                except (TypeError, json.JSONDecodeError) as error:
                    log_message(
                        "Skipping invalid event metadata",
                        event="crawler_parse_warning",
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue

                title = _clean(event.get("name"))
                url = _clean(event.get("url"))
                date_value = _clean(event.get("startDate"))
                location = event.get("location") or {}
                venue = _clean(location.get("name"))
                city, country_code = _geography(title)

                article = script.find_next("article", class_="mec-event-article")
                time_node = article.select_one(".mec-start-time") if article else None
                time_from = _time_24h(time_node.get_text(" ", strip=True) if time_node else None)

                try:
                    date_value = datetime.strptime(date_value, "%Y-%m-%d").date().isoformat()
                except (TypeError, ValueError):
                    date_value = None

                if not all((title, url, date_value, venue, city, country_code)):
                    log_message(
                        "Skipping event with incomplete required fields",
                        event="crawler_record_skipped",
                        url=url,
                        has_date=bool(date_value),
                        has_venue=bool(venue),
                        has_city=bool(city),
                        has_country_code=bool(country_code),
                    )
                    continue

                # These source rows explicitly say Hannover but attach Hamburg's
                # Elbphilharmonie and address; neither venue can be trusted.
                address = _clean(location.get("address")) or ""
                if city == "Hannover" and "Hamburg" in address:
                    log_message(
                        "Skipping event with conflicting source geography",
                        event="crawler_record_skipped",
                        url=url,
                        city=city,
                    )
                    continue

                records.append(
                    {
                        "title": title,
                        "date": date_value,
                        "url": url,
                        "time_from": time_from,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": _clean(event.get("description")),
                    }
                )

            next_offset = result.get("offset")
            next_start_date = result.get("end_date")
            if not result.get("has_more_event") or not scripts:
                break
            if (
                not isinstance(next_offset, int)
                or not isinstance(next_start_date, str)
                or (next_start_date, next_offset) == (start_date, offset)
            ):
                raise RuntimeError("Concert calendar pagination did not advance")
            start_date = next_start_date
            offset = next_offset

        log_message(
            "Concert scrape complete",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    LeifOveAndsnesCrawler().run()


if __name__ == "__main__":
    main()
