import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://oopperabaletti.fi/'
EVENTS_API = f'{SOURCE_URL}wp-json/ooppera/events'
SOURCE = 'Suomen kansallisooppera ja -baletti'
CITY = 'Helsinki'

VENUE_CITIES = {
    'Ouluhalli': 'Oulu',
    'Joensuu Areena': 'Joensuu',
    'Lappi Areena': 'Rovaniemi',
    'Hamina Bastioni': 'Hamina',
    'Tapiolan kirkko': 'Espoo',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def listing_events(session):
    # The API silently caps pages at 20 and its `hasMore` value is unreliable.
    # Asking from an early date exposes the site's still-published archive.
    params = {
        'perPage': 20,
        'page': 1,
        'startDate': '2000-01-01',
        'endDate': f'{date.today().year + 5}-12-31',
        'language': 'fi',
    }
    events = []
    while True:
        payload = get_json(session, EVENTS_API, params=params)
        page_events = payload.get('events') or []
        if not page_events:
            break
        events.extend(page_events)
        if len(page_events) < params['perPage']:
            break
        params['page'] += 1
    return events


def detail_description(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    blocks = soup.select_one('main .blocks')
    if not blocks:
        return None

    # Retain the editorial introduction, work/programme notes, and creator
    # credits. Exclude ticketing, cast, venue marketing, and service sections.
    wanted = []
    section = None
    for node in blocks.find_all(recursive=False):
        node_id = node.get('id')
        if node_id in ('esittely', 'tutustu-teoksiin', 'tekijat'):
            section = node_id
        elif node_id in (
            'hinnoittelu', 'osta-lippuja', 'rooleissa', 'nayttamo',
            'palvelut', 'sokeritehdas',
        ):
            section = None
        if section:
            text = clean_text(node.get_text('\n', strip=True))
            if text:
                wanted.append(text)
    return clean_text('\n\n'.join(wanted)) or None


def make_record(event, descriptions):
    title = clean_text(event.get('title_override') or event.get('title'))
    url = (event.get('link') or '').strip()
    venue = clean_text(event.get('location'))
    start = (event.get('start_date') or '').strip()
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?', start)
    if not title or not url or not venue or not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    # Most entries use halls in the Helsinki opera house, but the company also
    # publishes a small number of touring performances. Explicit tour venues
    # override the home-city default.
    city = CITY
    if venue.startswith('Alfonsin aula, kiertokäynti'):
        venue = 'Alfonsin aula'
    for venue_name, venue_city in VENUE_CITIES.items():
        if venue_name.casefold() in venue.casefold():
            venue = venue_name
            city = venue_city
            break

    description = descriptions.get(url) or clean_text(event.get('excerpt')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'country_code': 'FI',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    urls = sorted({event.get('link') for event in events if event.get('link')})
    descriptions = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, session, url): url for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = [make_record(event, descriptions) for event in events]
    records = [record for record in records if record]
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class OopperabalettiFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oopperabaletti_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OopperabalettiFiCrawler().run()


if __name__ == '__main__':
    main()
