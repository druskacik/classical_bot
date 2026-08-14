import re
from datetime import datetime
from urllib.parse import urljoin

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.uaeh.edu.mx/"
SOURCE = "Universidad Autónoma del Estado de Hidalgo"
EVENTS_API = urljoin(
    SOURCE_URL,
    "sistemas/gestionweb/webServices/gestionwebWs.php",
)

# The institutional feed covers campuses and outreach across Hidalgo. Only
# locations which identify a municipality, or well-known central UAEH venues,
# are mapped; ambiguous campus rooms are skipped rather than assigned Pachuca.
CITY_MARKERS = {
    "actopan": "Actopan",
    "atotonilco de tula": "Atotonilco de Tula",
    "ciudad sahagún": "Ciudad Sahagún",
    "huehuetla": "Huehuetla",
    "huejutla": "Huejutla de Reyes",
    "ixmiquilpan": "Ixmiquilpan",
    "mineral de la reforma": "Mineral de la Reforma",
    "pachuca": "Pachuca de Soto",
    "san agustín tlaxiaca": "San Agustín Tlaxiaca",
    "tepeapulco": "Tepeapulco",
    "tepeji del río": "Tepeji del Río de Ocampo",
    "tizayuca": "Tizayuca",
    "tlahuelilpan": "Tlahuelilpan",
    "tulancingo": "Tulancingo de Bravo",
    "zimapán": "Zimapán",
}

PACHUCA_VENUE_MARKERS = (
    "centro cultural universitario “la garza”",
    'centro cultural universitario "la garza"',
    "centro cultural universitario la garza",
    "torre de posgrado",
    "área académica de ciencias de la tierra",
)


def _clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _city_for_venue(venue):
    normalized = venue.casefold()
    for marker, city in CITY_MARKERS.items():
        if marker.casefold() in normalized:
            return city
    if any(marker in normalized for marker in PACHUCA_VENUE_MARKERS):
        return "Pachuca de Soto"
    return None


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class UaehCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="uaeh_edu_mx",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="MX",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "classical-concert-crawler/1.0",
            }
        )

    def scrape(self):
        log_message(
            "Fetching UAEH institutional events",
            event="crawler_url_fetch",
            url=EVENTS_API,
        )
        try:
            response = self.session.get(
                EVENTS_API,
                params={"idArea": "1", "accion": "obtenerEventos"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                "UAEH event request failed",
                event="crawler_fetch_failed",
                url=EVENTS_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        events = payload.get("eventos", {}).get("registros", [])
        records = []
        for event_data in events:
            title = _clean_text(event_data.get("nombre"))
            venue = _clean_text(event_data.get("lugar"))
            city = _city_for_venue(venue) if venue else None
            starts_at = _parse_datetime(event_data.get("fechaInicio"))
            event_id = event_data.get("id")
            if not title or not venue or not city or not starts_at or event_id is None:
                continue

            ends_at = _parse_datetime(event_data.get("fechaFin"))
            supplied_url = _clean_text(event_data.get("url")) or SOURCE_URL
            event_url = f"{urljoin(SOURCE_URL, supplied_url)}#evento-{event_id}"
            event_type = _clean_text(event_data.get("tipo"))

            records.append(
                {
                    "title": title,
                    "date": starts_at.date().isoformat(),
                    "url": event_url,
                    "time_from": starts_at.strftime("%H:%M"),
                    "time_to": ends_at.strftime("%H:%M") if ends_at else None,
                    "venue": venue,
                    "city": city,
                    "description": event_type or None,
                }
            )

        log_message(
            "UAEH events parsed",
            event="crawler_parse_completed",
            source_record_count=len(events),
            record_count=len(records),
        )
        return records


def main():
    UaehCrawler().run()


if __name__ == "__main__":
    main()
