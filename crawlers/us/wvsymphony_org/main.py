import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wvsymphony.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'season-calendar')
SOURCE = 'West Virginia Symphony Orchestra'
TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, offset=None):
    params = {'format': 'json', 'view': 'list'}
    if offset is not None:
        params['offset'] = offset
    response = session.get(CALENDAR_URL, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def listing_events(session):
    events = []
    seen_offsets = set()
    offset = None

    while True:
        payload = get_page(session, offset)
        events.extend(payload.get('upcoming') or [])
        events.extend(payload.get('past') or [])

        next_offset = (payload.get('pagination') or {}).get('nextPageOffset')
        if not next_offset or next_offset in seen_offsets:
            break
        seen_offsets.add(next_offset)
        offset = next_offset

    return events


def event_datetime(value):
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=TIMEZONE)
    except (ValueError, OSError, OverflowError):
        return None


def event_location(event):
    location = event.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    address_line1 = clean_text(location.get('addressLine1'))
    address = clean_text(location.get('addressLine2'))
    country = clean_text(location.get('addressCountry')).lower()
    if country and country not in ('united states', 'united states of america', 'us', 'usa'):
        return None, None

    # Squarespace stores locality as "City, ST, ZIP". Do not infer Charleston
    # when a touring event supplies a different or incomplete location.
    city = address.split(',', 1)[0].strip() if ',' in address else ''
    if not venue and address_line1.lower() == 'one clay square' and city == 'Charleston':
        venue = 'Clay Center for the Arts & Sciences of WV'
    if not venue or not city:
        return None, None
    return venue, city


def event_description(event):
    body = clean_text(event.get('body'))
    excerpt = clean_text(event.get('excerpt'))
    if body:
        return body
    return excerpt or None


def make_record(event):
    title = clean_text(event.get('title'))
    start = event_datetime(event.get('startDate'))
    venue, city = event_location(event)
    path = event.get('fullUrl')
    url = urljoin(SOURCE_URL, path) if path else ''
    if not title or not start or not venue or not city or not url:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': event_description(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []
    seen = set()

    for event in events:
        try:
            record = make_record(event)
        except (TypeError, ValueError) as error:
            log_message(
                'Failed to parse calendar event',
                event='crawler_item_failed',
                level='warning',
                url=urljoin(SOURCE_URL, event.get('fullUrl') or ''),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if not record:
            continue
        key = (record['url'], record['date'], record['time_from'])
        if key not in seen:
            seen.add(key)
            records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'], record['title'], record['url']
        ),
    )


class WvSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wvsymphony_org',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    WvSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
