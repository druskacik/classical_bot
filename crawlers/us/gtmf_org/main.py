import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://gtmf.org/'
SOURCE = 'Grand Teton Music Festival'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

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


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style, dialog'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').date().isoformat()
    except (TypeError, ValueError):
        return ''


def parse_time(value, all_day=False):
    if all_day:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').strftime('%H:%M')
    except (TypeError, ValueError):
        return None


def event_to_record(event):
    venue_data = event.get('venue') or {}
    title = clean_text(event.get('title'))
    event_date = parse_date(event.get('start_date'))
    url = event.get('url') or ''
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
        'time_from': parse_time(event.get('start_date'), event.get('all_day', False)),
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

    records = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        params = {**API_PARAMS, 'page': page}
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()

        try:
            total_pages = max(1, int(payload.get('total_pages', 1)))
        except (TypeError, ValueError):
            total_pages = 1

        for event in payload.get('events', []):
            record = event_to_record(event)
            if record:
                records.append(record)

        page += 1

    if not records:
        log_message(
            'No valid events found in GTMF API',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class GtmfOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gtmf_org',
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
    GtmfOrgCrawler().run()


if __name__ == '__main__':
    main()
