import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.albanysymphony.com/'
LISTING_URL = urljoin(SOURCE_URL, 'upcomingconcerts')
SOURCE = 'Albany Symphony'
TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_location(location):
    address_line = clean_text(location.get('addressLine2'))
    if not address_line:
        return ''
    return address_line.split(',', 1)[0].strip()


def parse_item(item):
    title = clean_text(item.get('title'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    start_millis = item.get('startDate')
    path = item.get('fullUrl')
    if not title or not venue or not city or not start_millis or not path:
        return None

    try:
        start = datetime.fromtimestamp(int(start_millis) / 1000, tz=TIMEZONE)
    except (TypeError, ValueError, OSError):
        return None

    description = clean_text(item.get('body') or item.get('excerpt')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
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
    params = {'format': 'json'}
    seen_offsets = set()
    records = []

    while True:
        response = session.get(LISTING_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
            record = parse_item(item)
            if record:
                records.append(record)

        pagination = payload.get('pagination') or {}
        offset = pagination.get('nextPageOffset') if pagination.get('nextPage') else None
        if offset is None or offset in seen_offsets:
            break
        seen_offsets.add(offset)
        params = {'format': 'json', 'offset': offset}

    unique = {
        (record['url'], record['date'], record['time_from']): record
        for record in records
    }
    result = sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'], record['title']),
    )
    if not result:
        log_message(
            'No events found in calendar feed',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return result


class AlbanySymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='albanysymphony_com',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    AlbanySymphonyComCrawler().run()


if __name__ == '__main__':
    main()
