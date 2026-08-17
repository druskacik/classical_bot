from datetime import date
import html
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wpsymphony.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Western Piedmont Symphony'

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


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    start = event.get('start_date_details') or {}
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    try:
        event_date = date(
            int(start['year']), int(start['month']), int(start['day'])
        ).isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    if not all((title, url, venue, city)) or not url.startswith(('http://', 'https://')):
        return None

    time_from = None
    if not event.get('all_day'):
        try:
            hour = int(start['hour'])
            minute = int(start['minutes'])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                time_from = f'{hour:02d}:{minute:02d}'
        except (KeyError, TypeError, ValueError):
            pass

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    end_year = date.today().year + 10
    params = {
        'start_date': '1900-01-01',
        'end_date': f'{end_year}-12-31',
        'per_page': 50,
        'page': 1,
    }

    records = []
    skipped_count = 0
    while True:
        response = session.get(EVENTS_API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for event in payload.get('events', []):
            record = parse_event(event)
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
            'Skipped events missing required fields',
            event='crawler_records_skipped',
            level='warning',
            url=EVENTS_API_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class WpSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wpsymphony_org',
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
    WpSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
