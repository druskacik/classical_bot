import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://auditorionacional.inaem.gob.es/es'
EVENTS_API = 'https://auditorionacional.inaem.gob.es/front-page-events.json'
SOURCE = 'Auditorio Nacional de Música'
CITY = 'Madrid'
DEFAULT_VENUE = 'Auditorio Nacional de Música'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_calendar_events(session):
    response = session.get(
        EVENTS_API,
        params={
            # The endpoint returns its complete retained archive for this range.
            'start': '2000-01-01',
            'end': f'{date.today().year + 5}-12-31',
        },
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError('Calendar API returned an unexpected payload')
    return payload


def detail_data(session, url, class_name):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    room = clean_text(soup.select_one('article .location'))
    if room:
        venue = f'{DEFAULT_VENUE} – {room}'
    elif class_name == 'sinfonica':
        venue = f'{DEFAULT_VENUE} – Sala Sinfónica'
    elif class_name == 'camara':
        venue = f'{DEFAULT_VENUE} – Sala de Cámara'
    else:
        venue = DEFAULT_VENUE

    # The left content column contains the promoter and full programme, while
    # the right column contains dates, ticket prices, and purchase links.
    description = clean_text(soup.select_one('article .col-md-8 .content')) or None
    return venue, description


def parse_event(event, details):
    title = clean_text(event.get('title') or event.get('description'))
    url = clean_text(event.get('url'))
    start = event.get('start')
    if not title or not url or not start:
        return None

    try:
        start_datetime = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None

    venue, description = details
    return {
        'title': title,
        'date': start_datetime.date().isoformat(),
        'url': url,
        'time_from': start_datetime.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': 'ES',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = get_calendar_events(session)

    detail_keys = {
        (clean_text(event.get('url')), clean_text(event.get('className')).lower())
        for event in events
        if event.get('url')
    }
    details = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_data, session, url, class_name): (url, class_name)
            for url, class_name in detail_keys
        }
        for future in as_completed(futures):
            url, class_name = futures[future]
            try:
                details[(url, class_name)] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                details[(url, class_name)] = (DEFAULT_VENUE, None)

    records = []
    for event in events:
        key = (
            clean_text(event.get('url')),
            clean_text(event.get('className')).lower(),
        )
        record = parse_event(event, details.get(key, (DEFAULT_VENUE, None)))
        if record:
            records.append(record)

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'], record['title']),
    )


class AuditorioNacionalInaemGobEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='auditorionacional_inaem_gob_es',
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
    AuditorioNacionalInaemGobEsCrawler().run()


if __name__ == '__main__':
    main()
