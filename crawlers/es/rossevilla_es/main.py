import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup, SoupStrainer

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://rossevilla.es/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/eventos'
SOURCE = 'Real Orquesta Sinfónica de Sevilla'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

MONTHS = {
    'ENE': 1, 'ENERO': 1, 'FEB': 2, 'FEBRERO': 2, 'MAR': 3, 'MARZO': 3,
    'ABR': 4, 'ABRIL': 4, 'MAY': 5, 'MAYO': 5, 'JUN': 6, 'JUNIO': 6,
    'JUL': 7, 'JULIO': 7, 'AGO': 8, 'AGOSTO': 8, 'SEP': 9, 'SEPT': 9,
    'SEPTIEMBRE': 9, 'OCT': 10, 'OCTUBRE': 10, 'NOV': 11,
    'NOVIEMBRE': 11, 'DIC': 12, 'DICIEMBRE': 12,
}
MONTH_PATTERN = '|'.join(sorted(MONTHS, key=len, reverse=True))

# ROSS performs mainly in Seville, but also tours. Only known Seville venues
# receive the home-city default; explicit place names remain authoritative.
SEVILLE_VENUES = (
    'maestranza', 'espacio turina', 'real alcázar', 'real alcazar',
    'cartuja center', 'teatro central', 'auditorio fibes', 'fibes',
    'iglesia de la anunciación', 'iglesia de la anunciacion',
    'hospital de los venerables', 'archivo de indias', 'plaza de españa',
)
CITY_MARKERS = {
    'sevilla': 'Sevilla', 'seville': 'Sevilla', 'córdoba': 'Córdoba',
    'cordoba': 'Córdoba', 'cádiz': 'Cádiz', 'cadiz': 'Cádiz',
    'málaga': 'Málaga', 'malaga': 'Málaga', 'granada': 'Granada',
    'huelva': 'Huelva', 'jaén': 'Jaén', 'jaen': 'Jaén', 'almería': 'Almería',
    'almeria': 'Almería', 'jerez': 'Jerez de la Frontera',
    'dos hermanas': 'Dos Hermanas', 'utrera': 'Utrera', 'carmona': 'Carmona',
    'sanlúcar': 'Sanlúcar de Barrameda', 'santander': 'Santander',
    'madrid': 'Madrid', 'barcelona': 'Barcelona', 'valencia': 'Valencia',
    'zaragoza': 'Zaragoza', 'bilbao': 'Bilbao', 'valladolid': 'Valladolid',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json(), response.headers


def event_urls(session):
    events = []
    page = 1
    while True:
        payload, headers = get_json(
            session, EVENTS_API,
            {'per_page': 100, 'page': page, '_fields': 'link,title'},
        )
        events.extend(
            (item.get('link'), clean_text((item.get('title') or {}).get('rendered')))
            for item in payload if item.get('link')
        )
        if page >= int(headers.get('X-WP-TotalPages', page)):
            break
        page += 1
    return list(dict.fromkeys(events))


def parse_dates(value):
    text = clean_text(value).upper().replace('.', '')
    year_match = re.search(r'\b(20\d{2})\b', text)
    if not year_match:
        return []
    year = int(year_match.group(1))
    results = []
    pattern = rf'((?:\d{{1,2}}\s*[/|,\-]\s*)*\d{{1,2}})\s*({MONTH_PATTERN})\b'
    for match in re.finditer(pattern, text):
        month = MONTHS[match.group(2)]
        for day_text in re.findall(r'\d{1,2}', match.group(1)):
            try:
                results.append(date(year, month, int(day_text)).isoformat())
            except ValueError:
                continue
    return list(dict.fromkeys(results))


def parse_time(value):
    match = re.search(r'(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)\s*[Hh]?', value or '')
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def resolve_city(venue):
    lowered = venue.casefold()
    for marker, city in CITY_MARKERS.items():
        if marker in lowered:
            return city
    if any(marker in lowered for marker in SEVILLE_VENUES):
        return 'Sevilla'
    return None


def detail_fields(soup):
    return [
        clean_text(node.get_text('\n', strip=True))
        for node in soup.select('main .jet-listing-dynamic-field__content')
        if clean_text(node.get_text(' ', strip=True))
    ]


def parse_event(url, html, api_title=''):
    # Pages contain large embedded Elementor and analytics payloads. Parsing
    # only the main element keeps the complete visible detail at low cost.
    soup = BeautifulSoup(html, 'html.parser', parse_only=SoupStrainer('main'))
    fields = detail_fields(soup)
    title = api_title
    if not title:
        title = fields[1] if len(fields) > 1 else (fields[0] if fields else '')

    date_index = next((i for i, value in enumerate(fields) if parse_dates(value)), None)
    if date_index is None:
        return []
    dates = parse_dates(fields[date_index])
    time_from = parse_time(fields[date_index])

    venue = ''
    for value in fields[date_index + 1:date_index + 4]:
        candidate = re.sub(r'\s*[|]\s*$', '', value).strip()
        if candidate and not parse_time(candidate) and candidate.casefold() != 'comprar':
            venue = candidate
            break
    city = resolve_city(venue) if venue else None
    if not title or not dates or not venue or not city:
        return []

    # Everything after the compact title/date/venue header is useful programme
    # material. De-duplicate repeated responsive widgets while preserving order.
    description_parts = []
    for value in fields[date_index + 1:]:
        normalized = re.sub(r'\s*[|]\s*$', '', value).strip()
        if (
            normalized and normalized not in description_parts
            and normalized not in {venue, title, time_from, 'Comprar', 'Volver'}
            and not (parse_time(normalized) and len(normalized) < 15)
        ):
            description_parts.append(normalized)
    description = clean_text('\n\n'.join(description_parts)) or None

    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in dates]


def fetch_event(url, title):
    # requests.Session mutates connection-pool state and is not shared across
    # worker threads. A standalone request gives each detail fetch isolation.
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(url, response.text, title)


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = event_urls(session)
    records = []
    # Elementor detail documents are large; modest concurrency avoids retaining
    # too many parse trees at once in memory on the scheduled worker.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_event, url, title): url
            for url, title in events
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['url']
    ))


class RossevillaEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rossevilla_es',
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
    RossevillaEsCrawler().run()


if __name__ == '__main__':
    main()
