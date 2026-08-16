import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Teatro Municipal de Santiago"
SOURCE_URL = "https://municipal.cl/"
CALENDAR_URL = "https://municipal.cl/cms/wp-admin/admin-ajax.php"
DEFAULT_VENUE = "Teatro Municipal de Santiago"
DEFAULT_CITY = "Santiago"

# Detail pages use the same visual style for a venue and for informational
# headings. These labels are metadata, not places.
NON_VENUE_LABELS = {
    "precio",
    "precios",
    "duracion",
    "duracion aproximada",
    "edad recomendada",
    "idioma",
    "subtitulos",
}


def _clean_text(value):
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _normalise_label(value):
    value = (value or "").lower()
    return value.translate(str.maketrans("áéíóúüñ", "aeiouun")).strip(" :")


def _valid_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _normalise_time(value):
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*", value)
    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?\s*", value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "p":
        hour += 12
    return f"{hour:02d}:{match.group(2)}"


def _function_date(value, start, end):
    event_date = _valid_date(value)
    if event_date and (not start or event_date >= start) and (not end or event_date <= end):
        return event_date
    # The calendar occasionally carries the previous season's year on an
    # otherwise valid function. Repair it only when month/day fits the event's
    # explicitly published range.
    if event_date and start and end:
        try:
            repaired = event_date.replace(year=start.year)
        except ValueError:
            return None
        if start <= repaired <= end:
            return repaired
    return None


def _detail_data(html, title):
    soup = BeautifulSoup(html, "html.parser")
    description_node = soup.select_one(".single-content__content .wysiwyg .text")
    description = None
    if description_node:
        description = _clean_text(description_node.get_text("\n", strip=True))

    venue = None
    for node in soup.select(".single-content__sidebar__item__title"):
        candidate = _clean_text(node.get_text(" ", strip=True))
        if candidate and _normalise_label(candidate) not in NON_VENUE_LABELS:
            venue = candidate
            break

    searchable = "\n".join(filter(None, (title, venue, description)))
    city = None
    if venue:
        # Pages for touring performances commonly state a locality in both the
        # venue name and the body (e.g. Teatro Municipal de Vicuña).
        patterns = (
            rf"{re.escape(venue)}[^\n.]{{0,100}}?,\s*([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÜÑáéíóúüñ -]+),\s*(?:Regi[oó]n|Chile)",
            r"(?:Teatro|Gimnasio|Centro Cultural|Anfiteatro|Sala)\s+(?:Municipal\s+)?(?:de|del)\s+([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÜÑáéíóúüñ -]{0,60}?)(?=\s*(?:[.,|\n]|$))",
        )
        for pattern in patterns:
            match = re.search(pattern, searchable)
            if match:
                city = _clean_text(match.group(1))
                break

    # Most pages omit a venue because this institution's own theatre is the
    # venue. Do not apply that default to explicitly touring/remote listings.
    touring = bool(re.search(r"\b(?:gira|extensi[oó]n cultural|municipal \+ cerca)\b", searchable, re.I))
    digital = bool(re.search(r"\b(?:cartelera digital|municipal delivery|online|streaming)\b", searchable, re.I))
    if venue and not city and venue == DEFAULT_VENUE:
        city = DEFAULT_CITY
    if not venue and not touring and not digital:
        venue, city = DEFAULT_VENUE, DEFAULT_CITY

    return description, venue, city


class MunicipalClCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="municipal_cl",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="CL",
        upload_target="potential",
        dedupe_subset=["title", "date", "time_from", "venue", "city", "url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "classical-bot/1.0 (+https://municipal.cl/)"})

    def _calendar(self):
        response = self.session.get(
            CALENDAR_URL,
            params={
                "action": "get_calendar",
                "salas_category": "",
                "date_ini": "1900-01-01",
                "category": "",
                "price": "",
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("data") or []

    def _fetch_detail(self, url):
        log_message("Fetching concert detail", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=45)
        response.raise_for_status()
        return response.text

    def scrape(self):
        events = self._calendar()
        details = {}
        urls = {event.get("url") for event in events if event.get("url")}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    details[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        "Concert detail fetch failed",
                        event="crawler_url_fetch_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for event in events:
            title = _clean_text(event.get("title"))
            url = event.get("url")
            if not title or not url or url not in details:
                continue
            description, venue, city = _detail_data(details[url], title)
            if not venue or not city:
                continue

            start = _valid_date(event.get("start_date"))
            end = _valid_date(event.get("end_date"))
            for function in event.get("functions") or []:
                event_date = _function_date(function.get("date_event"), start, end)
                if not event_date:
                    continue
                records.append(
                    {
                        "title": title,
                        "date": event_date.isoformat(),
                        "url": url,
                        "time_from": _normalise_time(function.get("hour")) or _normalise_time(event.get("time")),
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "description": description,
                    }
                )
        return records

def main():
    MunicipalClCrawler().run()


if __name__ == "__main__":
    main()
