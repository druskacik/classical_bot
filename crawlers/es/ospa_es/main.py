import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ospa.es/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/event'
SOURCE = 'Orquesta Sinfónica del Principado de Asturias'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'setiembre': 9, 'octubre': 10,
    'noviembre': 11, 'diciembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(
        r'(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})',
        clean_text(value).lower(),
    )
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None


def parse_location(value):
    location = clean_text(value).strip(' .,-')
    if not location:
        return None, None

    # OSPA formats locations as "Venue. City." (and occasionally with a
    # hyphen). Keeping the final component as the city also handles tours
    # without incorrectly applying the orchestra's Oviedo home venue.
    parts = [part.strip(' .,-') for part in re.split(r'\s*[.·]\s*|\s+-\s+', location)]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return None, None

    venue_terms = (
        'auditorio', 'teatro', 'palacio', 'casa de cultura', 'casa de la cultura',
        'polideportivo', 'colegiata', 'iglesia', 'basílica', 'basilica', 'catedral',
        'explanada', 'plaza', 'centro cultural', 'sala ', 'conservatorio',
    )
    first_is_venue = any(term in parts[0].lower() for term in venue_terms)
    last_is_venue = any(term in parts[-1].lower() for term in venue_terms)
    if last_is_venue and not first_is_venue:
        city_text = ' – '.join(parts[:-1])
        # Some touring entries prefix the municipality with a festival name,
        # or give a locality followed by its municipality.
        city = city_text.rsplit(',', 1)[-1].strip()
        if city.lower().startswith(('festival ', 'ciclo ', 'concierto ')) and ' de ' in city:
            city = city.rsplit(' de ', 1)[-1].strip()
        return parts[-1], city
    return ' – '.join(parts[:-1]), parts[-1]


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def listing_events(session):
    events = []
    page = 1
    while True:
        response = get_json(
            session,
            EVENTS_API,
            params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'},
        )
        events.extend(response.json())
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return events
        page += 1


def make_record(event, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text((event.get('title') or {}).get('rendered'))
    event_date = parse_date(soup.select_one('.ovaev-event-date'))
    venue, city = parse_location(soup.select_one('.ovaev-event-location'))
    url = event.get('link') or ''

    time_text = clean_text(soup.select_one('.ovaev-event-time'))
    times = re.findall(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)', time_text)
    time_from = ':'.join(times[0]) if times else None

    description = clean_text((event.get('content') or {}).get('rendered')) or None
    if not title or not event_date or not url or not venue or not city:
        return None
    return {
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
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_json, session, event['link']): event
            for event in events
            if event.get('link')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                record = make_record(event, future.result().text)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
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


class OspaEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ospa_es',
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
    OspaEsCrawler().run()


if __name__ == '__main__':
    main()
