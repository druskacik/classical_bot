import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://qcso.org/'
SOURCE = 'Quad City Symphony Orchestra'
API_URL = 'https://qcso.org/wp-json/tribe/events/v1/events'

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
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = re.sub(r'[ \t]+', ' ', text.replace('\xa0', ' '))
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_to_record(event):
    title = clean_text(event.get('title'))
    url = (event.get('url') or '').strip()
    start_value = event.get('start_date') or ''
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        venue_data = {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    try:
        start = datetime.strptime(start_value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    if not title or not url.startswith(('http://', 'https://')) or not venue or not city:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    params = {
        'per_page': 50,
        'page': 1,
        'start_date': '2000-01-01 00:00:00',
        'end_date': '2100-12-31 23:59:59',
        'status': 'publish',
    }

    records = []
    skipped_count = 0
    while True:
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []

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
            'Skipped events without required date or location fields',
            event='crawler_records_skipped',
            level='warning',
            url=API_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No valid event records found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class QcsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='qcso_org',
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
    QcsoOrgCrawler().run()


if __name__ == '__main__':
    main()
