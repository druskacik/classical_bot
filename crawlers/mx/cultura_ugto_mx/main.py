import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.cultura.ugto.mx/"
SOURCE = "Cultura UG, Universidad de Guanajuato"
API_TYPES = ("programa_musica", "programa_artes_escenicas")
LOCAL_TIMEZONE = ZoneInfo("America/Mexico_City")

# Cultura UG serves the whole state. These mappings are deliberately explicit:
# an event is skipped if its venue does not provide enough evidence for a city.
VENUE_CITIES = {
    "Teatro Juárez": "Guanajuato",
    "Teatro Principal": "Guanajuato",
    "Edificio Central UG, Salón del H. Consejo General Universitario": "Guanajuato",
    "Explanada de la Alhóndiga de Granaditas": "Guanajuato",
    "Auditorio ENMS Guanajuato": "Guanajuato",
    "Plazuela de San Roque": "Guanajuato",
    "Mesón de San Antonio": "Guanajuato",
    "Mesón de San Antonio - salón del Coro UG": "Guanajuato",
    "Mesón de San Antonio - sala de juntas": "Guanajuato",
    "Mesón de San Antonio - segundo patio": "Guanajuato",
    "Mina del Nopal": "Guanajuato",
    "Parroquia Jesuita": "Guanajuato",
    "Campus - León": "León",
    "Templo San Nicolás de Tolentino, León, Gto.": "León",
    "Parroquia del Señor de la Salud - León": "León",
    "Univerciudad UG - sede Barrio Arriba": "León",
    "ENMS Salvatierra": "Salvatierra",
    "Campus - Yuriria": "Yuriria",
    "Templo de San Antonio, Pénjamo, Gto.": "Pénjamo",
    "Auditorio de la ENMS San Luis de la Paz": "San Luis de la Paz",
    "Univerciudad UG - sede Casa El Nigromante": "San Miguel de Allende",
}


def _text_from_html(value):
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _textual_time(value):
    if not value or "varios" in value.lower():
        return None
    match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", value)
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def _venue_names(resource, included):
    relationship = resource.get("relationships", {}).get("field_sede", {}).get("data")
    if not relationship:
        return []
    relationships = relationship if isinstance(relationship, list) else [relationship]
    return [
        included[(item["type"], item["id"])]
        for item in relationships
        if (item["type"], item["id"]) in included
    ]


class CulturaUgtoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="cultura_ugto_mx",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="MX",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/vnd.api+json"})

    def _api_resources(self, content_type):
        url = f"{SOURCE_URL}jsonapi/node/{content_type}"
        params = {
            "include": "field_sede",
            "page[limit]": "50",
            "sort": "field_fechadelevento.value",
        }
        while url:
            log_message("Fetching event API page", event="crawler_url_fetch", url=url)
            try:
                response = self.session.get(url, params=params, timeout=30)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    "Event API request failed",
                    event="crawler_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            included = {
                (item["type"], item["id"]): item.get("attributes", {}).get("name")
                for item in payload.get("included", [])
                if item.get("attributes", {}).get("name")
            }
            yield from ((resource, included) for resource in payload.get("data", []))
            next_link = payload.get("links", {}).get("next", {}).get("href")
            url = next_link.replace("http://", "https://", 1) if next_link else None
            params = None

    def _records_for_resource(self, resource, included):
        attributes = resource.get("attributes", {})
        title = re.sub(r"\s+", " ", attributes.get("title", "")).strip()
        alias = (attributes.get("path") or {}).get("alias")
        venue_names = _venue_names(resource, included)
        if not title or not alias or len(venue_names) != 1:
            return []

        venue = venue_names[0]
        city = VENUE_CITIES.get(venue)
        if not city:
            return []

        description = _text_from_html((attributes.get("body") or {}).get("processed"))
        displayed_time = _textual_time(attributes.get("field_horariodelevento"))
        records = []
        for occurrence in attributes.get("field_fechadelevento") or []:
            try:
                start = datetime.fromisoformat(occurrence["value"]).astimezone(LOCAL_TIMEZONE)
            except (KeyError, TypeError, ValueError):
                continue

            duration = occurrence.get("duration")
            # Long ranges on this source are programme/season overview records,
            # not a reliable list of individual public performances.
            if isinstance(duration, (int, float)) and duration > 24 * 60:
                continue

            time_from = displayed_time
            time_to = None
            if isinstance(duration, (int, float)) and duration < 23 * 60:
                time_from = start.strftime("%H:%M")
                try:
                    end = datetime.fromisoformat(occurrence["end_value"]).astimezone(LOCAL_TIMEZONE)
                    time_to = end.strftime("%H:%M")
                except (KeyError, TypeError, ValueError):
                    pass

            records.append(
                {
                    "title": title,
                    "date": start.date().isoformat(),
                    "url": requests.compat.urljoin(SOURCE_URL, alias),
                    "time_from": time_from,
                    "time_to": time_to,
                    "venue": venue,
                    "city": city,
                    "description": description,
                }
            )
        return records

    def scrape(self):
        records = []
        for content_type in API_TYPES:
            for resource, included in self._api_resources(content_type):
                records.extend(self._records_for_resource(resource, included))
        log_message(
            "Cultura UG records parsed",
            event="crawler_parse_completed",
            record_count=len(records),
        )
        return records


def main():
    CulturaUgtoCrawler().run()


if __name__ == "__main__":
    main()
