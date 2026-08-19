import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operasaratoga.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Opera Saratoga'
TIMEZONE = ZoneInfo('America/New_York')

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


def city_from_location(location):
    address_line = clean_text(location.get('addressLine2'))
    if not address_line:
        return ''
    # Squarespace stores US locations as "City, State, postal code" (with
    # the second comma occasionally omitted). The first component is stable.
    return address_line.split(',', 1)[0].strip()


def parse_item(item):
    title = clean_text(item.get('title'))
    venue = clean_text((item.get('location') or {}).get('addressTitle'))
    city = city_from_location(item.get('location') or {})
    full_url = clean_text(item.get('fullUrl'))
    start_timestamp = item.get('startDate')

    # TBA and private-location entries do not provide a defensible venue and
    # city. They must not be emitted with invented placeholders.
    if not all((title, venue, city, full_url, start_timestamp)):
        return None
    if venue.casefold() in {'tba', 'tbd', 'to be announced'}:
        return None

    try:
        start = datetime.fromtimestamp(start_timestamp / 1000, tz=TIMEZONE)
    except (TypeError, ValueError, OSError, OverflowError):
        return None

    description = clean_text(item.get('body') or item.get('excerpt')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_calendar(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    records_by_id = {}
    next_url = f'{CALENDAR_URL}?format=json'
    visited_urls = set()
    while next_url and next_url not in visited_urls:
        visited_urls.add(next_url)
        response = session.get(next_url, timeout=60)
        response.raise_for_status()
        payload = response.json()

        for item in payload.get('upcoming', []) + payload.get('past', []):
            item_id = item.get('id')
            if not item_id or item_id in records_by_id:
                continue
            record = parse_item(item)
            if record:
                records_by_id[item_id] = record

        page_url = (payload.get('pagination') or {}).get('nextPageUrl')
        if page_url:
            separator = '&' if '?' in page_url else '?'
            next_url = urljoin(SOURCE_URL, f'{page_url}{separator}format=json')
        else:
            next_url = None

    records = sorted(
        records_by_id.values(),
        key=lambda item: (item['date'], item['time_from'], item['title'], item['url']),
    )
    if not records:
        log_message(
            'No valid calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return records


class OperaSaratogaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operasaratoga_org',
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
        return scrape_calendar()


def main():
    OperaSaratogaOrgCrawler().run()


if __name__ == '__main__':
    main()
