import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fondazionetoscanini.it/'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Fondazione Arturo Toscanini'

KNOWN_VENUE_CITIES = {
    'Auditorium di Milano Fondazione Cariplo': 'Milano',
    'Conservatorio di Reggio Emilia': 'Reggio Emilia',
    'CPM Arturo Toscanini': 'Parma',
    'Duomo di Modena': 'Modena',
    'Ridotto del Teatro Regio di Parma': 'Parma',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def listing_events(session):
    # Supplying both bounds makes the API return archived as well as future
    # occurrences. Recurring-event occurrences are expanded by this endpoint.
    url = EVENTS_API
    params = {
        'start_date': '2000-01-01 00:00:00',
        'end_date': '2100-12-31 23:59:59',
        'per_page': 50,
    }
    events = []
    while url:
        payload = get_json(session, url, params=params)
        events.extend(payload.get('events') or [])
        url = payload.get('next_rest_url')
        params = None
    return events


def resolve_location(event):
    location = event.get('venue') or {}
    if isinstance(location, list):
        location = next((item for item in location if isinstance(item, dict)), {})
    if not isinstance(location, dict):
        return None, None
    raw_venue = clean_text(location.get('venue'))
    city = clean_text(location.get('city'))

    if '|' in raw_venue:
        city_prefix, venue = (part.strip() for part in raw_venue.split('|', 1))
        city = city or city_prefix
    elif not city and ',' in raw_venue:
        city, venue = (part.strip() for part in raw_venue.split(',', 1))
    elif not city and re.search(r'\s+[–—]\s+', raw_venue):
        city, venue = re.split(r'\s+[–—]\s+', raw_venue, maxsplit=1)
    else:
        venue = raw_venue

    city = city or KNOWN_VENUE_CITIES.get(venue, '')

    # City labels sometimes include a province abbreviation, e.g. "Parma (PR)".
    city = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', city).strip()
    # A few legacy venue labels append a street address in parentheses.
    venue = re.sub(
        r'\s*\((?=(?:via|viale|piazza|parco|strada|borgo|largo)\b)[^)]*\)\s*$',
        '',
        venue,
        flags=re.IGNORECASE,
    ).strip()

    if not city or not venue or city.casefold() == venue.casefold():
        return None, None
    return venue, city


def detail_description(session, url, fallback=None):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    parts = []
    introduction = soup.select_one('.tribe-events-single > p')
    content = soup.select_one('.tribe-events-single-event-description')
    for node in (introduction, content):
        text = clean_text(node) if node else ''
        if text and text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or clean_text(fallback) or None


def make_record(event, description=None):
    title = clean_text(event.get('title'))
    url = html.unescape(str(event.get('url') or '')).strip()
    venue, city = resolve_location(event)
    start = clean_text(event.get('start_date'))
    try:
        start_at = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None

    if not title or not url or not venue or not city:
        return None
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description or clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(
                detail_description,
                session,
                html.unescape(str(event.get('url') or '')).strip(),
                event.get('description'),
            ): event
            for event in events
            if event.get('url')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                description = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=html.unescape(str(event.get('url') or '')).strip(),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                description = clean_text(event.get('description')) or None
            record = make_record(event, description)
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FondazionetoscaniniItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fondazionetoscanini_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
    FondazionetoscaniniItCrawler().run()


if __name__ == '__main__':
    main()
