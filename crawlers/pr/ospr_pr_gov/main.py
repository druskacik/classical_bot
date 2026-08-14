import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.ospr.pr.gov/"
EVENTS_URL = urljoin(SOURCE_URL, "eventos")
SOURCE = "Orquesta Sinfónica de Puerto Rico"
TIME_ZONE = ZoneInfo("America/Puerto_Rico")
DEFAULT_VENUE = "Sala Sinfónica Pablo Casals"
DEFAULT_CITY = "San Juan"

VENUE_PATTERNS = (
    (r"Centro de Bellas Artes Luis A\. Ferr[eé]\s*-\s*Sala de Festivales Antonio Paoli", "Sala de Festivales Antonio Paoli"),
    (r"Sala de Festivales Antonio Paoli(?: del Centro de Bellas Artes Luis A\. Ferr[eé])?", "Sala de Festivales Antonio Paoli"),
    (r"Sala de Festivales CBA", "Sala de Festivales Antonio Paoli"),
    (r"Centro de Bellas Artes Luis A\. Ferr[eé]\s*-\s*Sala Sinf[oó]nica", DEFAULT_VENUE),
    (r"Sala Sinf[oó]nica Pablo Casals", DEFAULT_VENUE),
    (r"Conservatorio de M[uú]sica de Puerto Rico\s*-?\s*Sala Jes[uú]s Mar[ií]a Sanrom[aá] del Teatro Guillermo y Bertita Mart[ií]nez", "Sala Jesús María Sanromá"),
    (r"Teatro Bertita y Guillermo L\. Mart[ií]nez", "Teatro Bertita y Guillermo L. Martínez"),
    (r"Conservatorio de M[uú]sica de Puerto Rico", "Conservatorio de Música de Puerto Rico"),
    (r"Teatro de la UPR(?: en R[ií]o Piedras)?", "Teatro de la Universidad de Puerto Rico"),
    (r"[ÁA]rea Recreativa Lisandro Lugo", "Área Recreativa Lisandro Lugo"),
    (r"Distrito T-?Mobile", "Distrito T-Mobile"),
)

NON_PHYSICAL_PATTERNS = re.compile(
    r"\b(?:concierto|recital|campechada) virtual\b|Facebook Live|Canal de YouTube|"
    r"transmitir[aá]|transmisi[oó]n|concierto grabado|edici[oó]n de la grabaci[oó]n",
    re.IGNORECASE,
)


def _plain_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def _venue_and_city(title: str, description: str) -> tuple[str, str] | None:
    text = f"{title}\n{description}"
    if NON_PHYSICAL_PATTERNS.search(text):
        return None

    for pattern, venue in VENUE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            if venue == "Área Recreativa Lisandro Lugo":
                return venue, "San Germán"
            return venue, DEFAULT_CITY

    # The orchestra's regular seasons and the Casals Festival use its home
    # concert hall. Touring events are never assigned this fallback.
    if re.search(r"\b(?:en tu pueblo|Mayag[uü]ez|Ponce|Caguas|Arecibo|Bayam[oó]n)\b", text, re.IGNORECASE):
        return None
    return DEFAULT_VENUE, DEFAULT_CITY


def _event_record(item: dict) -> dict | None:
    title = re.sub(r"\s+", " ", item.get("title", "")).strip()
    start_ms = item.get("startDate")
    if not title or not isinstance(start_ms, (int, float)):
        return None

    description = _plain_text(item.get("body", ""))
    location = _venue_and_city(title, description)
    if location is None:
        return None

    start = datetime.fromtimestamp(start_ms / 1000, tz=TIME_ZONE)
    end_ms = item.get("endDate")
    end = datetime.fromtimestamp(end_ms / 1000, tz=TIME_ZONE) if isinstance(end_ms, (int, float)) else None
    venue, city = location
    return {
        "title": title,
        "date": start.date().isoformat(),
        "url": urljoin(SOURCE_URL, item.get("fullUrl") or f"eventos/{item.get('urlId', '')}"),
        "time_from": start.time().replace(tzinfo=None).isoformat(timespec="minutes"),
        "time_to": end.time().replace(tzinfo=None).isoformat(timespec="minutes") if end else None,
        "venue": venue,
        "city": city,
        "description": description or None,
    }


class OsprCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="ospr_pr_gov",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="PR",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        seen_ids = set()
        offset = None

        while True:
            params = {"format": "json"}
            if offset is not None:
                params["offset"] = offset
            log_message("Fetching events page", event="crawler_url_fetch", url=EVENTS_URL)
            response = requests.get(EVENTS_URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()

            page_items = payload.get("upcoming", []) + payload.get("past", [])
            new_items = [item for item in page_items if item.get("id") not in seen_ids]
            for item in new_items:
                seen_ids.add(item.get("id"))
                record = _event_record(item)
                if record is not None:
                    records.append(record)

            pagination = payload.get("pagination") or {}
            next_offset = pagination.get("nextPageOffset")
            if not pagination.get("nextPage") or next_offset is None or not new_items:
                break
            offset = next_offset

        log_message("Events parsed", event="crawler_records_parsed", record_count=len(records))
        return records


def main():
    OsprCrawler().run()


if __name__ == "__main__":
    main()
