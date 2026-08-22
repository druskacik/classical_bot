import html
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.saocarlos.pt/'
SOURCE = 'Teatro Nacional de São Carlos / Orquestra Sinfónica Portuguesa'
API_URL = 'https://repeater.bondlayer.com/fetch'

PROJECT_ID = 's5dyvlsauld0nknf'
SESSIONS_COLLECTION = 'cTP80BJ1Kf1DfIuZ'
SESSIONS_REPEATER = 'cOeB8eyZntXxYj1J'
PAGE_SIZE = 100

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Origin': 'https://www.saocarlos.pt',
    'Referer': 'https://www.saocarlos.pt/',
}

# Session records do not contain cities. These are the named venues used by the
# public archive. Unknown venues are deliberately skipped instead of assigning
# the theatre's Lisbon home city to a touring performance.
VENUE_LOCATIONS = {
    'Academia das Ciências de Lisboa': ('Lisboa', 'PT'),
    'Capitólio': ('Lisboa', 'PT'),
    'Capitólio (Festival Around Classic)': ('Lisboa', 'PT'),
    'Casa da Música': ('Porto', 'PT'),
    'Castelo Kuressaare': ('Kuressaare', 'EE'),
    'Centro Cultural de Belém': ('Lisboa', 'PT'),
    'Centro Cultural e Congressos': ('Caldas da Rainha', 'PT'),
    'Centro Cultural e de Congressos': ('Caldas da Rainha', 'PT'),
    'Centro Cultural e de Congressos das Caldas da Rainha': ('Caldas da Rainha', 'PT'),
    'Centro Cultural Olga Cadaval': ('Sintra', 'PT'),
    'Centro de Artes e Espetáculos': ('Figueira da Foz', 'PT'),
    'Cinema São Jorge': ('Lisboa', 'PT'),
    "Cinema-Teatro Joaquim d'Almeida": ('Montijo', 'PT'),
    'Cineteatro de Alter do Chão': ('Alter do Chão', 'PT'),
    'Coliseu dos Recreios': ('Lisboa', 'PT'),
    'Coliseu Porto Ageas': ('Porto', 'PT'),
    'Convento de São Francisco': ('Coimbra', 'PT'),
    'Escola EB1 de Telheiras': ('Lisboa', 'PT'),
    'Escadaria do Santuário de Nossa Senhora da Atalaia': ('Montijo', 'PT'),
    'Fundação Calouste Gulbenkian': ('Lisboa', 'PT'),
    'Igreja de São Francisco': ('Évora', 'PT'),
    'Igreja de São Roque': ('Lisboa', 'PT'),
    'LA CC - Casa de São Mamede': ('Lisboa', 'PT'),
    'Largo de São Carlos': ('Lisboa', 'PT'),
    'Palácio dos Liláses': ('Lisboa', 'PT'),
    'Palácio Nacional da Ajuda': ('Lisboa', 'PT'),
    'Palácio Nacional de Mafra': ('Mafra', 'PT'),
    'Palácio Nacional de Queluz': ('Queluz', 'PT'),
    'Palácio Nacional de Sintra': ('Sintra', 'PT'),
    'Panorama': ('Alcobaça', 'PT'),
    'Ponto C': ('Penafiel', 'PT'),
    'Quinta da Regaleira': ('Sintra', 'PT'),
    'Reitoria da Universidade Nova de Lisboa': ('Lisboa', 'PT'),
    'Reitoria Universidade Nova de Lisboa': ('Lisboa', 'PT'),
    'São Luiz Teatro Municipal': ('Lisboa', 'PT'),
    'Sociedade Filarmónica Recreio Alverquense': ('Alverca do Ribatejo', 'PT'),
    'Teatro Aberto': ('Lisboa', 'PT'),
    'Teatro Camões': ('Lisboa', 'PT'),
    'Teatro das Figuras': ('Faro', 'PT'),
    'Teatro Micaelense': ('Ponta Delgada', 'PT'),
    'Teatro Municipal António Pinheiro': ('Tavira', 'PT'),
    'Teatro Municipal de Bragança': ('Bragança', 'PT'),
    'Teatro Municipal de Ourém': ('Ourém', 'PT'),
    'Teatro Municipal de Vila Real': ('Vila Real', 'PT'),
    'Teatro Municipal Garcia de Resende': ('Évora', 'PT'),
    'Teatro Municipal Joaquim Benite': ('Almada', 'PT'),
    'Teatro Nacional de São Carlos (Foyer)': ('Lisboa', 'PT'),
    'Teatro Nacional de São João': ('Porto', 'PT'),
    'Teatro Tivoli BBVA': ('Lisboa', 'PT'),
    'Teatro Variedades': ('Lisboa', 'PT'),
    'Teatro Virgínia': ('Torres Novas', 'PT'),
    'Theatro Circo': ('Braga', 'PT'),
    'Tivoli BBVA': ('Lisboa', 'PT'),
    'Carnegie Hall': ('New York', 'US'),
    'Real Gabinete Português de Leitura': ('Rio de Janeiro', 'BR'),
}


def localized(value):
    if isinstance(value, dict):
        return value.get('all') or ''
    return value or ''


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalize_venue(value):
    venue = clean_text(value).strip()
    venue = re.sub(r'\s+', ' ', venue)
    aliases = {
        'Coliseu Porto AGEAS': 'Coliseu Porto Ageas',
        'Castelo Kuressaare': 'Castelo Kuressaare',
        'São Luiz Teatro Municipal': 'São Luiz Teatro Municipal',
    }
    return aliases.get(venue, venue)


def api_payload(start, end):
    return {
        # Bondlayer includes this cache-buster in its own browser requests.
        'hash': str(time.time_ns()),
        'target': 'production',
        'geoData': {'lat': 0, 'lon': 0},
        'searchQuery': '',
        'favorites': {},
        'repeater': {
            'liveFetch': True,
            'detail': False,
            'sorts': [{'attr': 'datetime_date', 'direction': 'asc'}],
            'version': 1,
            'pagination': {
                'enabled': False,
                'perPage': None,
                'pageRangeDisplayed': 6,
                'marginPagesDisplayed': 0,
            },
            'id': SESSIONS_REPEATER,
            'limit': {'enabled': True, 'start': start, 'end': end},
            'filters': [],
            'collection': SESSIONS_COLLECTION,
        },
        'locale': 'pt',
        'contentId': '0',
        'projectId': PROJECT_ID,
    }


def fetch_sessions(session):
    items = []
    related = {}
    start = 0
    while True:
        response = session.post(
            API_URL,
            json=api_payload(start, start + PAGE_SIZE),
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        batch = data.get('items') or []
        related.update(data.get('related') or {})
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return items, related


def event_url(program):
    slug = localized(program.get('_slug')).strip('/')
    return f'{SOURCE_URL}program/{slug}/' if slug else ''


def programme_description(program):
    parts = []
    for field in ('text_intro', 'text_setlist', 'text_description'):
        text = clean_text(localized(program.get(field)))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_session(item, related):
    program = related.get(item.get('ref_program')) or {}
    title = clean_text(localized(program.get('text_display_title')))
    if not title:
        title = clean_text(localized(program.get('_title')))
        title = re.sub(r'^\d{2}/\d{2}_', '', title)

    timestamp = item.get('datetime_date') or ''
    try:
        occurrence = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None

    venue = normalize_venue(localized(item.get('text_location')))
    location = VENUE_LOCATIONS.get(venue)
    url = event_url(program)
    if not title or not venue or not location or not url:
        if venue and not location:
            log_message(
                'Skipping concert with unknown touring venue',
                event='crawler_item_skipped',
                level='warning',
                url=url or SOURCE_URL,
                venue=venue,
            )
        return None

    city, country_code = location
    return {
        'title': title,
        'date': occurrence.date().isoformat(),
        'url': url,
        'time_from': occurrence.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': programme_description(program),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items, related = fetch_sessions(session)
    records = [parse_session(item, related) for item in items]
    records = [record for record in records if record]
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['venue']),
    )


class OrquestraSinfonicaPortuguesaPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orquestrasinfonicaportuguesa_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
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
    OrquestraSinfonicaPortuguesaPtCrawler().run()


if __name__ == '__main__':
    main()
