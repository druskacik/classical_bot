import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rfgalicia.org/gl/'
PROGRAM_URL = f'{SOURCE_URL}programacion/'
API_URL = 'https://www.rfgalicia.org/wp-json/wp/v2/posts'
SOURCE = 'Real Filharmonía de Galicia'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'gl-ES,gl;q=0.9,es;q=0.8',
}

CITY_MARKERS = {
    'a coruña': 'A Coruña',
    'ferrol': 'Ferrol',
    'lugo': 'Lugo',
    'ourense': 'Ourense',
    'pontevedra': 'Pontevedra',
    'ribadeo': 'Ribadeo',
    'vigo': 'Vigo',
    'vilagarcía': 'Vilagarcía de Arousa',
    'vilagarcia': 'Vilagarcía de Arousa',
}

SANTIAGO_VENUES = (
    'auditorio de galicia',
    'casa das máquinas',
    'igrexa de santa maría de mercé de conxo',
    'centro comercial área central',
    'igrexa de santo agostiño',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_city(venue):
    normalized = venue.casefold()
    for marker, city in CITY_MARKERS.items():
        if marker in normalized:
            return city
    if any(marker in normalized for marker in SANTIAGO_VENUES):
        return 'Santiago de Compostela'
    return None


def post_id(url):
    values = parse_qs(urlparse(url).query).get('p') or []
    return values[0] if values and values[0].isdigit() else None


def parse_card(card):
    content = card.select_one('.contenido_programacion')
    link = card.select_one('a[href]')
    if not content or not link:
        return []

    values = list(content.stripped_strings)
    date_index = next(
        (index for index, value in enumerate(values) if re.fullmatch(r'\d{2}-\d{2}-\d{4}', value)),
        None,
    )
    if date_index is None or date_index < 2:
        return []

    title = clean_text(values[0])
    venue = clean_text(values[date_index - 1])
    city = resolve_city(venue)
    try:
        event_date = datetime.strptime(values[date_index], '%d-%m-%Y').date().isoformat()
    except ValueError:
        return []
    if not title or not venue or not city:
        return []

    time_text = next((value for value in values[date_index + 1:] if value.startswith('Hora:')), '')
    times = re.findall(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)', time_text)
    times = [f'{hour.zfill(2)}:{minute}' for hour, minute in times] or [None]
    url = requests.compat.urljoin(PROGRAM_URL, link['href'])
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'ES',
            'description': None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
            '_post_id': post_id(url),
        }
        for time_from in times
    ]


def fetch_description(session, identifier):
    response = session.get(f'{API_URL}/{identifier}', timeout=45)
    response.raise_for_status()
    return clean_text(response.json().get('content', {}).get('rendered')) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(PROGRAM_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    records = [record for card in soup.select('.contenedor_post') for record in parse_card(card)]

    descriptions = {}
    identifiers = {record['_post_id'] for record in records if record['_post_id']}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_description, session, identifier): identifier
            for identifier in identifiers
        }
        for future in as_completed(futures):
            identifier = futures[future]
            try:
                descriptions[identifier] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=f'{API_URL}/{identifier}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        record['description'] = descriptions.get(record.pop('_post_id'))
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class RfgaliciaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rfgalicia_org',
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
        return get_concerts()


def main():
    RfgaliciaOrgCrawler().run()


if __name__ == '__main__':
    main()
