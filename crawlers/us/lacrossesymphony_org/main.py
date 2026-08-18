import html
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lacrossesymphony.org/'
SOURCE = 'La Crosse Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
DEFAULT_CITY = 'La Crosse'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

API_PARAMS = {
    'per_page': 50,
    'start_date': '2000-01-01 00:00:00',
    'end_date': '2100-12-31 23:59:59',
    'status': 'publish',
}


def clean_html(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def parse_start(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def fetch_events(session):
    events = []
    page = 1

    while True:
        params = {**API_PARAMS, 'page': page}
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        page_events = payload.get('events')
        if not isinstance(page_events, list):
            raise ValueError('Events Calendar API response has no events list')
        events.extend(page_events)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    return events


def event_to_record(event):
    title = clean_html(event.get('title'))
    url = str(event.get('url') or '').strip()
    event_date, time_from = parse_start(event.get('start_date'))

    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None
    venue = clean_html(venue_data.get('venue'))
    city = clean_html(venue_data.get('city')) or DEFAULT_CITY

    if not title or not event_date or not url.startswith(('http://', 'https://')):
        return None
    if not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_html(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = fetch_events(session)

    records = []
    skipped_count = 0
    for event in events:
        record = event_to_record(event)
        if record:
            records.append(record)
        else:
            skipped_count += 1

    if skipped_count:
        log_message(
            'Skipped events without a usable date, venue, or city',
            event='crawler_records_skipped',
            level='warning',
            url=API_URL,
            record_count=skipped_count,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class LaCrosseSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lacrossesymphony_org',
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
        return scrape_concerts()


def main():
    LaCrosseSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
