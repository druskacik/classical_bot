from datetime import datetime
import html
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://rentickets.org/'
SOURCE = 'Renaissance Theatre'
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
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    for node in soup.select('script, style, noscript'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value, all_day=False):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), None if all_day else parsed.strftime('%H:%M')


def event_to_record(event):
    title = clean_text(event.get('title'))
    event_date, time_from = parse_start(event.get('start_date'), event.get('all_day', False))
    url = str(event.get('url') or '').strip()

    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    if not all((title, event_date, url, venue, city)):
        return None
    if not url.startswith(('http://', 'https://')):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
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
                'per_page': 50,
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
            record = event_to_record(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete required fields',
                    event='crawler_event_skipped',
                    level='warning',
                    url=str(event.get('url') or API_URL),
                    event_id=event.get('id'),
                )

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not events:
            break
        page += 1

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class RenticketsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rentickets_org',
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
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return scrape_events()


def main():
    RenticketsOrgCrawler().run()


if __name__ == '__main__':
    main()
