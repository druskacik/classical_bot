import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operacluj.ro/'
SOURCE = 'Opera Națională Română Cluj-Napoca'
EVENTS_ROUTE = '/tribe/events/v1/events'

# The site's pretty REST route is rejected by its web server, while the
# equivalent WordPress rest_route query is public and stable.
API_URL = SOURCE_URL
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'ro-RO,ro;q=0.9,en;q=0.7',
}

# First-party Tribe Events category IDs for concrete or potentially qualifying
# performances. Ambiguous categories (musical, matinee, generic performance,
# tango) deliberately remain in this candidate feed for classifier review.
# Guided visits, online performances, fairs, workshops, conferences and
# exhibitions are excluded.
PERFORMANCE_CATEGORY_IDS = (
    3, 14, 37, 41, 43, 44, 45, 46, 49, 50, 51, 52, 53, 96, 182, 232,
    238, 255, 266, 268, 318, 338, 340, 354, 358, 362, 364, 366, 371, 381,
    385,
)

DEFAULT_VENUE = 'Opera Națională Română Cluj-Napoca'
DEFAULT_CITY = 'Cluj-Napoca'


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value))
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_pages(session):
    params = {
        'rest_route': EVENTS_ROUTE,
        'page': 1,
        'per_page': 50,
        'start_date': '2000-01-01',
        'end_date': '2100-12-31',
        'categories': ','.join(map(str, PERFORMANCE_CATEGORY_IDS)),
    }
    while True:
        response = session.get(API_URL, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        yield from payload.get('events') or []

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1


def event_location(event):
    venue_data = event.get('venue') or []
    if isinstance(venue_data, list):
        venue_data = venue_data[0] if venue_data else {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    if venue and city:
        return venue, city

    # This is the institution's own programme and its records normally omit
    # Tribe venue objects. Only use the home-stage default when the page does
    # not identify a touring/off-site location.
    description = clean_text(event.get('description'))
    touring_markers = (
        'turneu', 'în deplasare', 'in deplasare', 'găzduit de', 'gazduit de',
    )
    if any(marker in description.lower() for marker in touring_markers):
        return '', ''
    return DEFAULT_VENUE, DEFAULT_CITY


def make_record(event):
    url = clean_text(event.get('url'))
    # The API publishes a second English translation as a separate occurrence.
    if '/en/' in url:
        return None

    title = clean_text(event.get('title'))
    start = clean_text(event.get('start_date'))
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):\d{2}', start)
    venue, city = event_location(event)
    if not title or not url or not match or not venue or not city:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None if event.get('all_day') else f'{match.group(2)}:{match.group(3)}'
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': clean_text(event.get('description')) or None,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    try:
        for event in event_pages(session):
            record = make_record(event)
            if record:
                records.append(record)
    except (requests.RequestException, ValueError, TypeError) as error:
        log_message(
            'Failed to retrieve event feed',
            event='crawler_feed_failed',
            level='error',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class OperaClujRoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operacluj_ro',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RO',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaClujRoCrawler().run()


if __name__ == '__main__':
    main()
