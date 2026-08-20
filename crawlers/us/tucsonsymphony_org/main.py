import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tucsonsymphony.org/'
SOURCE = 'Tucson Symphony Orchestra'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
FIRST_EVENT_DATE = '2017-01-01 00:00:00'
LAST_EVENT_DATE = '2100-12-31 23:59:59'
PER_PAGE = 50

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
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def event_to_record(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    event_date, time_from = parse_start(event.get('start_date'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    if not all((title, event_date, url, venue, city)):
        return None
    if not str(url).startswith(('https://', 'http://')):
        return None

    description = clean_text(event.get('description')) or clean_text(event.get('excerpt')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None if event.get('all_day') else time_from,
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
            'start_date': FIRST_EVENT_DATE,
            'end_date': LAST_EVENT_DATE,
            'per_page': PER_PAGE,
            'page': page,
            'status': 'publish',
        }
        try:
            response = session.get(EVENTS_API_URL, params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Unable to fetch Tucson Symphony events',
                event='crawler_request_failed',
                level='error',
                url=EVENTS_API_URL,
                page=page,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        events = payload.get('events', [])
        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 0)
        if not events or page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No parseable Tucson Symphony events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class TucsonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tucsonsymphony_org',
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
    TucsonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
