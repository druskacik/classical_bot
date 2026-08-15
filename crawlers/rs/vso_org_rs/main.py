import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Vojvođanski simfonijski orkestar"
SOURCE_URL = "https://vso.org.rs/"
EVENTS_API = "https://vso.org.rs/wp-json/tribe/events/v1/events"


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    text = "\n".join(soup.stripped_strings)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def infer_venue(description: str | None) -> tuple[str | None, str | None]:
    """Infer only venues explicitly named in legacy event descriptions."""
    text = (description or "").casefold()
    patterns = [
        (r"kolar(?:čeva|ceva) zadužbina|kolarac", "Kolarčeva zadužbina", "Beograd"),
        (r"srpsko narodno pozorište|српско народно позориште|\bsnp\b", "Srpsko narodno pozorište", "Novi Sad"),
        (r"novosadsk[a-zčćžšđ]* sinagog|sinagog|синагог|synagogue", "Sinagoga", "Novi Sad"),
        (r"park(?:u)? prisajedinjenja", "Park Prisajedinjenja", "Novi Sad"),
        (r"bulevar(?:u)? mihaila pupina", "Bulevar Mihaila Pupina", "Novi Sad"),
        (r"gradska koncertna dvorana", "Gradska koncertna dvorana Novi Sad", "Novi Sad"),
        (r"mts dvorana", "MTS dvorana", "Beograd"),
        (r"sava centar", "Sava Centar", "Beograd"),
        (r"narodno pozorište sombor", "Narodno pozorište Sombor", "Sombor"),
    ]
    for pattern, venue, city in patterns:
        if re.search(pattern, text):
            return venue, city
    return None, None


def parse_event(event: dict) -> dict | None:
    try:
        start = datetime.fromisoformat(event["start_date"])
    except (KeyError, TypeError, ValueError):
        return None

    title = clean_text(event.get("title"))
    url = event.get("url")
    description = clean_text(event.get("description"))
    venue_data = event.get("venue")

    venue = city = None
    if isinstance(venue_data, dict):
        venue = clean_text(venue_data.get("venue"))
        city = clean_text(venue_data.get("city"))
    if not venue or not city:
        inferred_venue, inferred_city = infer_venue(description)
        venue = venue or inferred_venue
        city = city or inferred_city

    if not title or not url or not venue or not city:
        return None

    end_time = None
    try:
        end = datetime.fromisoformat(event["end_date"])
        end_time = end.strftime("%H:%M:%S")
    except (KeyError, TypeError, ValueError):
        pass

    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": url,
        "time_from": start.strftime("%H:%M:%S"),
        "time_to": end_time,
        "venue": venue,
        "city": city,
        "description": description,
    }


class VsoOrgRsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="vso_org_rs",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="RS",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "time_from", "venue"],
    )

    def scrape(self) -> list[dict]:
        records = []
        page = 1
        total_pages = 1
        session = requests.Session()

        while page <= total_pages:
            params = {
                "start_date": "2000-01-01",
                "end_date": "2100-12-31",
                "per_page": 50,
                "page": page,
            }
            log_message("Fetching event API page", event="crawler_url_fetch", url=EVENTS_API, page=page)
            response = session.get(EVENTS_API, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            total_pages = int(payload.get("total_pages", 0))

            for event in payload.get("events", []):
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        "Skipping event without a defensible venue or required field",
                        event="crawler_record_skipped",
                        url=event.get("url"),
                    )
            page += 1

        # The API exposes Serbian and English translations as separate events.
        # Keep Serbian URLs first so BaseCrawler's occurrence deduplication
        # retains the canonical local-language page.
        records.sort(key=lambda record: "/en/" in record["url"])
        log_message("Event API scrape completed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    VsoOrgRsCrawler().run()


if __name__ == "__main__":
    main()
