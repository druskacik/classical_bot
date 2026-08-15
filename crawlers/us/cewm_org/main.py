import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cewm.org/'
SOURCE = 'Close Encounters With Music'
API_URL = 'https://cewm.org/wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = html.unescape(str(value))
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text(separator, strip=True)
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_start(value, all_day=False):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), None if all_day else parsed.strftime('%H:%M')


def event_to_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    event_date, time_from = parse_start(event.get('start_date'), event.get('all_day'))

    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    if not all((title, event_date, url, venue, city)):
        return None
    if not url.startswith(('https://', 'http://')):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description'), separator='\n') or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    params = {
        'per_page': 50,
        'page': 1,
        'start_date': '2000-01-01',
        'end_date': '2100-12-31',
        'status': 'publish',
    }
    records = []
    skipped_count = 0

    while True:
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events', [])

        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1

    if skipped_count:
        log_message(
            'Skipped events without required fields',
            event='crawler_records_skipped',
            level='warning',
            url=API_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No valid events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class CewmOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cewm_org',
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
    CewmOrgCrawler().run()


if __name__ == '__main__':
    main()
