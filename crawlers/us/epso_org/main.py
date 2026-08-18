from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.epso.org/'
SOURCE = 'El Paso Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

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
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def parse_start_date(value):
    try:
        parsed = datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def event_to_record(event):
    title = clean_text(event.get('title'))
    url = str(event.get('url') or '').strip()
    event_date, event_time = parse_start_date(event.get('start_date'))
    description = clean_text(event.get('description')) or None
    venue_data = event.get('venue')
    venue = clean_text(venue_data.get('venue')) if isinstance(venue_data, dict) else ''
    city = clean_text(venue_data.get('city')) if isinstance(venue_data, dict) else ''

    # A small number of otherwise complete concert records omit the API venue
    # object even though their first-party description names EPSO's home hall.
    category_ids = {
        category.get('id')
        for category in event.get('categories', [])
        if isinstance(category, dict)
    }
    if not venue and 242 in category_ids and description and 'Plaza Theatre' in description:
        venue = 'Plaza Theatre'
        city = 'El Paso'
    if not title or not event_date or not url.startswith(('http://', 'https://')):
        return None
    if not venue or not city:
        return None

    if event.get('all_day'):
        event_time = None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        params = {
            'per_page': 50,
            'page': page,
            'status': 'publish',
            'start_date': '1900-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
        }
        try:
            response = session.get(API_URL, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'EPSO events API request failed',
                event='crawler_request_failed',
                level='error',
                url=API_URL,
                page=page,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        events = payload.get('events')
        if not isinstance(events, list):
            raise ValueError('EPSO events API response does not contain an events list')

        skipped_count = 0
        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        if skipped_count:
            log_message(
                'Skipped EPSO events without required fields',
                event='crawler_records_skipped',
                level='warning',
                url=API_URL,
                page=page,
                record_count=skipped_count,
            )

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class EpsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='epso_org',
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
    EpsoOrgCrawler().run()


if __name__ == '__main__':
    main()
