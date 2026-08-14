import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.unm.edu.pe/'
SOURCE = 'Universidad Nacional de Música'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-PE,es;q=0.9',
}

# These first-party categories contain performances or plausible performance
# events. The resulting feed remains mixed (notably masterclasses,
# presentations, and multi-event listings), so it is sent for classification.
CANDIDATE_CATEGORY_SLUGS = {
    'clases-maestras',
    'concierto',
    'experimentacion-sonora',
    'homenajes',
    'presentaciones',
    'presentaciones-2',
    'recitales',
    'titulaciones',
}

VENUE_PATTERN = re.compile(
    r'\b(?:auditorio|biblioteca|casa|catedral|centro cultural|conservatorio|'
    r'gran teatro|hemiciclo|iglesia|local(?: principal| caman[aá])?|museo|'
    r'parque|patio|plaza|plazuela|sala(?: de usos m[uú]ltiples)?|sum|teatro)\b',
    re.IGNORECASE,
)

PERUVIAN_CITIES = (
    'Arequipa', 'Ayacucho', 'Cajamarca', 'Chachapoyas', 'Chiclayo', 'Cusco',
    'Huancayo', 'Huánuco', 'Ica', 'Iquitos', 'Lima', 'Piura', 'Pucallpa',
    'Puerto Maldonado', 'Tacna', 'Trujillo',
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def candidate_category_ids(session):
    response = get_json(
        session,
        f'{API_URL}/categorias-eventos',
        {'per_page': 100, 'hide_empty': 'false'},
    )
    terms = response.json()
    ids = [str(term['id']) for term in terms if term.get('slug') in CANDIDATE_CATEGORY_SLUGS]
    if not ids:
        raise ValueError('No candidate event categories were found')
    return ids


def api_events(session, category_ids):
    page = 1
    while True:
        response = get_json(
            session,
            f'{API_URL}/eventos',
            {
                'per_page': 100,
                'page': page,
                'categorias-eventos': ','.join(category_ids),
                'orderby': 'date',
                'order': 'desc',
            },
        )
        yield from response.json()
        if page >= int(response.headers.get('X-WP-TotalPages', '1')):
            break
        page += 1


def detail_fields(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    fields = [
        clean_text(node)
        for node in soup.select('.jet-listing-dynamic-field__content')
    ]
    date_text = next(
        (value for value in fields if re.fullmatch(r'Fecha:\s*\d{1,2}/\d{1,2}/\d{4}', value)),
        '',
    )
    time_text = next((value for value in fields if value.casefold().startswith('hora:')), '')
    if not date_text:
        return None, None
    event_date = datetime.strptime(date_text.split(':', 1)[1].strip(), '%d/%m/%Y').date().isoformat()
    time_from = None
    match = re.search(r'(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?', time_text, re.IGNORECASE)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3).casefold() == 'p':
            hour += 12
        time_from = f'{hour:02d}:{match.group(2)}'
    return event_date, time_from


def location_from_description(description):
    lines = [clean_text(line).strip(' .') for line in description.split('\n') if clean_text(line)]
    candidates = []
    for line in lines:
        value = re.sub(r'^(?:lugar|local)\s*:\s*', '', line, flags=re.IGNORECASE)
        if VENUE_PATTERN.search(value):
            candidates.append(value)
    if not candidates:
        return None, None

    location = min(candidates, key=len)
    venue = re.split(r'\s*[,(](?=\s*(?:calle|jir[oó]n|jr\.|av\.|avenida|ubicad|\d))', location, 1, re.I)[0]
    venue = clean_text(venue).strip(' ,.;:-')
    if not venue or not VENUE_PATTERN.search(venue):
        return None, None

    city = 'Lima'
    for city_name in PERUVIAN_CITIES:
        if re.search(rf'\b{re.escape(city_name)}\b', location, re.IGNORECASE):
            city = city_name
            break
    return venue, city


def make_record(session, item):
    title = clean_text(item.get('title', {}).get('rendered'))
    url = clean_text(item.get('link'))
    description = clean_text(
        BeautifulSoup(item.get('content', {}).get('rendered') or '', 'html.parser')
    )
    if not title or not url or not description:
        return None
    event_date, time_from = detail_fields(session, url)
    venue, city = location_from_description(description)
    if not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'PE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class UnmEduPeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='unm_edu_pe',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        category_ids = candidate_category_ids(session)
        items = list(api_events(session, category_ids))
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(make_record, session, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape UNM event detail',
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
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    UnmEduPeCrawler().run()


if __name__ == '__main__':
    main()
