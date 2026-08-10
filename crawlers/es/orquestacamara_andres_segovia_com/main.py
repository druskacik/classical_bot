import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orquestacamara-andres-segovia.com/'
SOURCE = 'Orquesta de Cámara Andrés Segovia'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'setiembre': 9, 'octubre': 10,
    'noviembre': 11, 'diciembre': 12,
}


def clean_html(value):
    if not value:
        return ''
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def explicit_dates(description, fallback_year):
    """Read dates following a FECHA label; some API start dates are post dates."""
    match = re.search(
        r'(?is)\bFECHA\s*:?[ \t]*(?:[a-záéíóú]+\s+)?'
        r'(\d{1,2})(?:\s+y\s+(?:[a-záéíóú]+\s+)?(\d{1,2}))?'
        r'\s+de\s+([a-záéíóú]+)(?:\s+de\s+(20\d{2}))?',
        description,
    )
    if not match:
        return []
    month = MONTHS.get(match.group(3).lower())
    year = int(match.group(4) or fallback_year)
    if not month:
        return []
    results = []
    for day_value in (match.group(1), match.group(2)):
        if not day_value:
            continue
        try:
            results.append(date(year, month, int(day_value)).isoformat())
        except ValueError:
            return []
    return results


def event_dates(event, description):
    start = event.get('start_date_details') or {}
    fallback_year = start.get('year')
    dates = explicit_dates(description, fallback_year) if fallback_year else []
    if dates:
        return dates
    try:
        return [date(
            int(start['year']), int(start['month']), int(start['day'])
        ).isoformat()]
    except (KeyError, TypeError, ValueError):
        return []


def event_time(event, description):
    match = re.search(
        r'(?i)\b(?:HORARIO|HORA)\s*:?\s*(?:a\s+las\s+)?'
        r'([01]?\d|2[0-3])\s*[:,.]\s*([0-5]\d)',
        description,
    )
    if match:
        return f'{int(match.group(1)):02d}:{match.group(2)}'
    details = event.get('start_date_details') or {}
    if event.get('all_day'):
        return None
    try:
        return f"{int(details['hour']):02d}:{int(details['minutes']):02d}"
    except (KeyError, TypeError, ValueError):
        return None


def event_location(event, description):
    venue_data = event.get('venue')
    if isinstance(venue_data, dict):
        venue = str(venue_data.get('venue') or '').strip()
        city = str(venue_data.get('city') or '').strip()
        city = city_from_text(city) or city
        if not city:
            city = city_from_text(f'{venue} {description}')
        if venue and city:
            return venue, city

    location_match = re.search(
        r'(?is)\bLOCALIZACI[ÓO]N\s*/?\s*DIRECCI[ÓO]N\s*:?[ \t]*(.+?)'
        r'(?=\n\s*(?:HORARIO|HORA|LINK|FECHA|PROGRAMA)\b)',
        description,
    )
    location_text = location_match.group(1).strip() if location_match else ''
    combined = f'{event.get("title", "")}\n{location_text}'
    city = city_from_text(combined)
    venue = venue_from_text(combined)
    return (venue, city) if venue and city else None


def city_from_text(value):
    normalized = value.lower()
    city_patterns = (
        ('San Lorenzo de El Escorial', r'san lorenzo de el escorial'),
        ('Pozuelo de Alarcón', r'pozuelo de alarc[oó]n'),
        ('Majadahonda', r'majadahonda'),
        ('Madrid', r'\bmadrid\b|arganzuela'),
    )
    for city, pattern in city_patterns:
        if re.search(pattern, normalized):
            return city
    return None


def venue_from_text(value):
    known = (
        ('Nave de Terneras', r'nave de terneras'),
        ('Casa de Cultura Carmen Conde', r'casa de cultura [«"]?carmen conde'),
        ('Teatro Auditorio de San Lorenzo de El Escorial', r'teatro auditorio de san lorenzo'),
        ('Auditorio Nacional de Música', r'auditorio nacional de m[uú]sica'),
        ('Fundación BBVA, Palacio del Marqués de Salamanca', r'fundaci[oó]n bbva.*palacio del marqu[eé]s de salamanca'),
        ('Teatro MIRA', r'teatro mira'),
    )
    normalized = value.lower().replace('\n', ' ')
    for venue, pattern in known:
        if re.search(pattern, normalized):
            return venue
    return None


def parse_event(event):
    title = clean_html(event.get('title'))
    url = str(event.get('url') or '').strip()
    description = clean_html(event.get('description'))
    location = event_location(event, description)
    dates = event_dates(event, description)
    if not title or not url or not location or not dates:
        return []

    venue, city = location
    time_from = event_time(event, description)
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in dates]


class OrquestacamaraAndresSegoviaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestacamara_andres_segovia_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(
                API_URL,
                params={
                    'per_page': 50,
                    'start_date': '2000-01-01 00:00:00',
                    'end_date': '2100-12-31 23:59:59',
                    'status': 'publish',
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch OCAS events API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for event in payload.get('events', []):
            records.extend(parse_event(event))
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ))


def main():
    OrquestacamaraAndresSegoviaComCrawler().run()


if __name__ == '__main__':
    main()
