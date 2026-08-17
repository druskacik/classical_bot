import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://readingsymphony.org/'
SOURCE = 'Reading Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
MAIN_EVENTS_CATEGORY_ID = 22

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_html(value):
    if not value:
        return None
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def clean_inline(value):
    text = clean_html(value)
    return re.sub(r'\s+', ' ', text).strip() if text else None


def parse_event(event):
    title = clean_inline(event.get('title'))
    url = event.get('url')
    venue_data = event.get('venue') or {}
    venue = clean_inline(venue_data.get('venue'))
    city = clean_inline(venue_data.get('city'))

    try:
        start = datetime.strptime(event.get('start_date', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    if not title or not url or not venue or not city:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_html(event.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    params = {
        'categories': MAIN_EVENTS_CATEGORY_ID,
        'start_date': '2000-01-01',
        'end_date': '2100-12-31',
        'per_page': 50,
        'page': 1,
    }
    records = []

    while True:
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events', [])

        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete required fields',
                    event='crawler_event_skipped',
                    level='warning',
                    url=event.get('url'),
                )

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1

    if not records:
        log_message(
            'No events found in the main events API feed',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ReadingSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='readingsymphony_org',
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
    ReadingSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
