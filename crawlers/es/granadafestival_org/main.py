import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.granadafestival.org/es/'
PROGRAMME_URL = f'{SOURCE_URL}programa'
EVENTS_URL = 'https://www.granadafestival.org/ajax/eventosDia'
SOURCE = 'Festival Internacional de Música y Danza de Granada'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': PROGRAMME_URL,
}

# The main programme is in Granada, but FEX also tours the province. These
# labels are supplied by the site itself and must take precedence over the
# festival's home-city default.
VENUE_CITIES = {
    'ALHENDÍN.': 'Alhendín',
    'ATARFE.': 'Atarfe',
    'BUBIÓN.': 'Bubión',
    'GALERA.': 'Galera',
    'GÓJAR.': 'Gójar',
    'HUÉTOR VEGA.': 'Huétor Vega',
    'ORCE.': 'Orce',
    'PULIANAS.': 'Pulianas',
    'PURULLENA.': 'Purullena',
    'Pampaneira.': 'Pampaneira',
    'Puerto de MOTRIL.': 'Motril',
    'SALOBREÑA.': 'Salobreña',
    'SANTA FE.': 'Santa Fe',
    'VILLANUEVA DE LAS TORRES,': 'Villanueva de las Torres',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_city(venue):
    for prefix, city in VENUE_CITIES.items():
        if venue.startswith(prefix):
            return city
    if 'La Calahorra' in venue:
        return 'La Calahorra'
    return 'Granada'


def parse_datetime(value):
    match = re.match(r'(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}))?', value)
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%d/%m/%Y').date().isoformat()
    except ValueError:
        return None, None
    return event_date, match.group(2)


def listing_records(session):
    response = session.post(
        EVENTS_URL,
        data={
            'fecha': '', 'todos': 0, 'festival': 0, 'fex': 0, 'cmf': 0,
            'genero': 0, 'escenario': 0, 'gratuito': 0, 'nombre': '',
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    soup = BeautifulSoup(payload.get('html', ''), 'html.parser')
    records = []
    for row in soup.select('div.row'):
        link = row.select_one('a.card-title[href]')
        location_icon = row.select_one('.fa-location-dot')
        clock_icon = row.select_one('.fa-clock')
        title = clean_text(link)
        venue = clean_text(location_icon.parent) if location_icon else ''
        datetime_text = clean_text(clock_icon.parent) if clock_icon else ''
        event_date, time_from = parse_datetime(datetime_text)
        url = link.get('href', '').strip() if link else ''
        if (
            not title or not event_date or not url or not venue
            or venue.lower() == 'sin ubicación'
        ):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': resolve_city(venue),
            'country_code': 'ES',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return clean_text(soup.select_one('.descripcionDetalleEvento')) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = listing_records(session)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, session, record['url']): record
            for record in records
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Granada Festival event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique_records = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique_records.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class GranadaFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='granadafestival_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    GranadaFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
