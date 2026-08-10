import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gijon.es/es/eventos'
SOURCE = 'Agenda del Ayuntamiento de Gijón / Xixón'
CITY = 'Gijón'
LISTING_API = 'https://drupal.gijon.es/es/listado_eventos_tes4/?_format=json'
DETAIL_BASE = 'https://drupal.gijon.es/es/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'es-ES,es;q=0.9',
}

MUSIC_TERMS = (
    'música', 'musica', 'clásica', 'clasica', 'concierto', 'ópera', 'opera',
    'piano', 'sinfón', 'sinfon', 'orquesta', 'ensemble', 'coral', 'gospel',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


def is_music_event(item):
    searchable = ' '.join(
        clean_text(item.get(field))
        for field in ('titulo', 'tipo', 'etiquetas', 'programa')
    ).lower()
    return clean_text(item.get('tipo')).lower() == 'música' or any(
        term in searchable for term in MUSIC_TERMS
    )


def first_iso_date(value):
    match = re.search(r'\b\d{4}-\d{2}-\d{2}\b', value or '')
    if not match:
        return None
    try:
        return date.fromisoformat(match.group()).isoformat()
    except ValueError:
        return None


def detail_url(alias):
    path = (alias or '').strip('/')
    return urljoin(DETAIL_BASE, f'{path}/?_format=json') if path else ''


def public_url(alias):
    path = (alias or '').strip('/')
    return urljoin(SOURCE_URL, f'/es/{path}') if path else ''


def detail_description(payload):
    bodies = payload.get('body') or []
    if not bodies:
        return None
    body = bodies[0]
    return clean_text(body.get('processed') or body.get('value')) or None


def make_record(item, detail=None):
    title = clean_text(item.get('titulo'))
    event_date = first_iso_date(item.get('fecha_inicio'))
    venue = clean_text(
        item.get('titulo_directorio') or item.get('field_lo_name')
    )
    url = public_url(item.get('alias'))
    if not title or not event_date or not venue or not url:
        return None

    time_from = clean_text(item.get('hora_inicio')) or None
    if time_from and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from):
        time_from = None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'ES',
        'description': detail_description(detail or {}),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = [item for item in get_json(session, LISTING_API) if is_music_event(item)]

    aliases = {item.get('alias') for item in items if item.get('alias')}
    details = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_json, session, detail_url(alias)): alias
            for alias in aliases
        }
        for future in as_completed(futures):
            alias = futures[future]
            try:
                details[alias] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=public_url(alias),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    seen = set()
    for item in items:
        record = make_record(item, details.get(item.get('alias')))
        if not record:
            continue
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        if key not in seen:
            seen.add(key)
            records.append(record)
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class GijonEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gijon_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    GijonEsCrawler().run()


if __name__ == '__main__':
    main()
