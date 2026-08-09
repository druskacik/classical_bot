import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://extension.usach.cl/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/mec-events'
SOURCE = 'Extensión Usach'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-CL,es;q=0.9,en;q=0.6',
}

# The calendar primarily serves Greater Santiago, whose communes are often
# used as the locality in addresses. It also publishes touring performances,
# so explicit cities elsewhere in Chile take precedence over any home default.
LOCALITIES = (
    'Santiago', 'Estación Central', 'Cerro Navia', 'La Granja', 'Maipú',
    'Providencia', 'Ñuñoa', 'Las Condes', 'La Reina', 'Quilicura',
    'Pudahuel', 'Quinta Normal', 'Recoleta', 'Independencia', 'San Miguel',
    'San Joaquín', 'Lo Prado', 'Lo Espejo', 'Peñalolén', 'Puente Alto',
    'Vitacura', 'Valparaíso', 'Viña del Mar', 'Rancagua', 'Talca',
    'Chillán', 'Concepción', 'Temuco', 'Valdivia', 'Osorno', 'Puerto Montt',
    'La Serena', 'Coquimbo', 'Antofagasta', 'Iquique', 'Arica', 'Copiapó',
)


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json(), response.headers


def listing_events(session):
    events = []
    page = 1
    while True:
        payload, headers = get_json(
            session,
            EVENTS_API,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': 'id,link,title,content',
            },
        )
        events.extend(payload)
        if page >= int(headers.get('X-WP-TotalPages', page)):
            return events
        page += 1


def event_schema(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            candidates = payload.get('@graph') or [payload]
        else:
            candidates = payload
        if isinstance(candidates, dict):
            candidates = [candidates]
        for item in candidates or []:
            kinds = item.get('@type', []) if isinstance(item, dict) else []
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if 'Event' in kinds:
                time_node = soup.select_one('.mec-single-event-time')
                time_match = re.search(
                    r'\b([01]?\d|2[0-3]):([0-5]\d)\b',
                    clean_text(time_node.get_text(' ', strip=True) if time_node else ''),
                )
                if time_match:
                    item['_time_from'] = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
                return item
    return None


def extract_city(location):
    if not isinstance(location, dict):
        return None
    address = location.get('address') or ''
    if isinstance(address, dict):
        explicit = clean_text(address.get('addressLocality'))
        address = ' '.join(clean_text(value) for value in address.values())
        if explicit:
            return explicit
    haystack = clean_text(f"{location.get('name', '')} {address}")
    for locality in LOCALITIES:
        if re.search(rf'(?<!\w){re.escape(locality)}(?!\w)', haystack, re.IGNORECASE):
            return locality
    # Campus venues without a touring locality are unambiguously in the
    # university's home commune, Estación Central.
    if re.search(r'\b(?:Usach|Universidad de Santiago)\b', haystack, re.IGNORECASE):
        return 'Estación Central'
    return None


def make_record(event, schema):
    if not schema:
        return None
    title = clean_text(schema.get('name') or (event.get('title') or {}).get('rendered'))
    url = clean_text(schema.get('url') or event.get('link'))
    start = clean_text(schema.get('startDate'))
    match = re.match(r'^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2}))?', start)
    location = schema.get('location') or {}
    if isinstance(location, list):
        location = next((item for item in location if isinstance(item, dict)), {})
    venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
    city = extract_city(location)
    if not title or not url or not match or not venue or not city:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None
    description = clean_text((event.get('content') or {}).get('rendered'))
    if not description:
        description = clean_text(schema.get('description')) or None
    time_from = (
        f'{match.group(2)}:{match.group(3)}' if match.group(2)
        else schema.get('_time_from')
    )
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'CL',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry, pool_maxsize=6))
    events = listing_events(session)
    records = []

    def fetch(event):
        response = session.get(event['link'], timeout=45)
        response.raise_for_status()
        return make_record(event, event_schema(response.text))

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch, event): event for event in events if event.get('link')}
        for future in as_completed(futures):
            event = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class ExtensionUsachClCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='extension_usach_cl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CL',
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
    ExtensionUsachClCrawler().run()


if __name__ == '__main__':
    main()
