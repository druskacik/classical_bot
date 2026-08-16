import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chq.org/'
EVENTS_URL = f'{SOURCE_URL}things-to-do/events/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Chautauqua Institution'
DEFAULT_CITY = 'Chautauqua'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

# These categories describe off-campus or non-performance listings for which
# the institution's home-city default is not defensible.
NONLOCAL_CATEGORIES = {'chq-travels', 'regional-attractions'}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = re.sub(r'[ \t]+', ' ', text.replace('\xa0', ' '))
    return re.sub(r' *\n *|\n{3,}', lambda match: '\n\n' if '\n\n' in match.group() else '\n', text).strip()


def parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return ''


def parse_time(value, all_day=False):
    if all_day or not value:
        return None
    match = re.search(r'\b(\d{2}):(\d{2}):\d{2}\b', str(value))
    if not match:
        return None
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def event_to_record(event):
    title = clean_text(event.get('title'))
    event_date = parse_date(event.get('start_date'))
    url = str(event.get('url') or '').strip()
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    categories = {
        category.get('slug')
        for category in event.get('categories') or []
        if category.get('slug')
    }

    city = clean_text(venue_data.get('city'))
    if not city and venue and not categories.intersection(NONLOCAL_CATEGORIES):
        city = DEFAULT_CITY

    if not all((title, event_date, url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(event.get('start_date'), event.get('all_day', False)),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    # The API otherwise defaults to a rolling two-year window. An explicit
    # wide range retains all past occurrences that CHQ continues to publish.
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '1900-01-01',
                'end_date': '2100-12-31',
                'status': 'publish',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []

        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not events:
            break
        page += 1

    if not records:
        log_message(
            'No usable events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ChqOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chq_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_events()


def main():
    ChqOrgCrawler().run()


if __name__ == '__main__':
    main()
