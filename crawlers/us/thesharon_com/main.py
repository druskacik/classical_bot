import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://thesharon.com/'
SOURCE = 'The Sharon Performing Arts Center'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
CITY = 'The Villages'
COUNTRY_CODE = 'US'
PAGE_SIZE = 50

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
    text = str(value)
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def event_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = parse_datetime(event.get('start_date'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))

    if not title or not start or not venue or not url.startswith(('http://', 'https://')):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': PAGE_SIZE,
                'page': page,
                'start_date': '2000-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
                'status': 'publish',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []

        for event in events:
            record = event_record(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete required fields',
                    event='crawler_event_skipped',
                    level='warning',
                    url=clean_text(event.get('url')) or API_URL,
                    event_id=event.get('id'),
                )

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not events:
            break
        page += 1

    if not records:
        log_message(
            'No events found in API',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class TheSharonComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='thesharon_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    TheSharonComCrawler().run()


if __name__ == '__main__':
    main()
