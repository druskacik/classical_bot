import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.eriephil.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Erie Philharmonic'
TIME_ZONE = ZoneInfo('America/New_York')

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_datetime(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None


def location_fields(location):
    if not isinstance(location, dict):
        return '', ''

    venue = clean_text(location.get('addressTitle'))
    address_line = clean_text(location.get('addressLine2'))
    city = address_line.split(',', 1)[0].strip() if address_line else ''
    return venue, city


def description_from_item(item):
    parts = []
    for field in ('excerpt', 'body'):
        text = clean_text(item.get(field))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def record_from_item(item):
    title = clean_text(item.get('title'))
    start = event_datetime(item.get('startDate'))
    venue, city = location_fields(item.get('location'))
    event_url = urljoin(SOURCE_URL, item.get('fullUrl') or '')

    if not all((title, start, venue, city, item.get('fullUrl'))):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': event_url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description_from_item(item),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    url = f'{CALENDAR_URL}?format=json'
    seen_pages = set()
    records = []

    while url and url not in seen_pages:
        seen_pages.add(url)
        response = session.get(url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in payload.get('upcoming', []) + payload.get('past', []):
            record = record_from_item(item)
            if record:
                records.append(record)

        next_url = payload.get('pagination', {}).get('nextPageUrl')
        if next_url:
            separator = '&' if '?' in next_url else '?'
            url = urljoin(SOURCE_URL, f'{next_url}{separator}format=json')
        else:
            url = None

    if not records:
        log_message(
            'No valid calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class EriePhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eriephil_org',
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
    EriePhilOrgCrawler().run()


if __name__ == '__main__':
    main()
