import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://agrupaciones.ofb.gov.co/'
SOURCE = 'Agrupaciones Orquesta Filarmónica de Bogotá'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/mec-events')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/131.0 Safari/537.36'
    ),
    'Accept-Language': 'es-CO,es;q=0.9',
}

CITY_PATTERNS = {
    'Bogotá': r'\bbogot[aá]\b',
    'Barranquilla': r'\bbarranquilla\b',
    'Bucaramanga': r'\bbucaramanga\b',
    'Cali': r'\bcali\b',
    'Cartagena': r'\bcartagena\b',
    'Chía': r'\bch[ií]a\b',
    'Cúcuta': r'\bc[uú]cuta\b',
    'Ibagué': r'\bibagu[eé]\b',
    'Manizales': r'\bmanizales\b',
    'Medellín': r'\bmedell[ií]n\b',
    'Neiva': r'\bneiva\b',
    'Pereira': r'\bpereira\b',
    'Santa Marta': r'\bsanta\s+marta\b',
    'Tunja': r'\btunja\b',
    'Villavicencio': r'\bvillavicencio\b',
    'Zipaquirá': r'\bzipaquir[aá]\b',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    elif '<' in str(value) and '>' in str(value):
        value = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    else:
        value = str(value)
    value = html.unescape(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response


def catalogue(session):
    items = []
    page = 1
    while True:
        response = get_json(
            session,
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,link,title,content,mec_category',
            },
        )
        items.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return items
        page += 1


def event_schema(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def infer_city(title, venue, address, description):
    evidence = ' '.join((title, venue, address, description))
    # Avenida Ciudad de Cali is a major road in Bogotá, not evidence that the
    # performance is in Cali.
    evidence = re.sub(r'\b(?:av(?:enida)?\.?)\s+ciudad\s+de\s+cali\b', '', evidence, flags=re.I)
    for city, pattern in CITY_PATTERNS.items():
        if re.search(pattern, evidence, re.IGNORECASE):
            return city
    # This is the calendar of Bogotá-based OFB ensembles. Touring appearances
    # identify their destination in the title, venue, address, or description.
    return 'Bogotá'


def valid_venue(value):
    if not value:
        return False
    normalized = value.strip(' "')
    if normalized.lower() in {'por definir', 'virtual', 'online'}:
        return False
    # MEC sometimes stores a street address in both the venue-name and address
    # fields. An address is not a defensible venue, so omit that occurrence.
    return not re.fullmatch(
        r'(?:cl\.?|calle|cr\.?|cra\.?|carrera|av\.?|avenida)\s*\d.*',
        normalized,
        re.IGNORECASE,
    )


def parse_item(item, page_html):
    schema = event_schema(page_html)
    if not schema:
        return None

    title = clean_text(schema.get('name')) or clean_text(item.get('title', {}).get('rendered'))
    location = schema.get('location') if isinstance(schema.get('location'), dict) else {}
    venue = clean_text(location.get('name')).strip(' "')
    address_value = location.get('address')
    if isinstance(address_value, dict):
        address = clean_text(' '.join(str(value) for value in address_value.values()))
    else:
        address = clean_text(address_value)
    description = clean_text(item.get('content', {}).get('rendered')) or clean_text(
        schema.get('description')
    )
    url = clean_text(item.get('link'))

    try:
        start = datetime.fromisoformat(clean_text(schema.get('startDate')).replace('Z', '+00:00'))
    except ValueError:
        return None

    if not title or not url or not valid_venue(venue):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': infer_city(title, venue, address, description),
        'country_code': 'CO',
        'description': description or None,
    }


def fetch_item(item):
    session = requests.Session()
    session.headers.update(HEADERS)
    response = get_json(session, item['link'])
    return parse_item(item, response.text)


class AgrupacionesOfbGovCoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='agrupaciones_ofb_gov_co',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CO',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = catalogue(session)
        records = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_item, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Agrupaciones OFB event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=item.get('link'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    AgrupacionesOfbGovCoCrawler().run()


if __name__ == '__main__':
    main()
