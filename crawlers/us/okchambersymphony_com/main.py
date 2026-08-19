import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.okchambersymphony.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'concertschedule')
SOURCE = 'Oklahoma Chamber Symphony'
TIME_ZONE = ZoneInfo('America/Chicago')

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
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_location(location):
    address_line = clean_text((location or {}).get('addressLine2'))
    return address_line.split(',', 1)[0].strip() if address_line else ''


def parse_item(item):
    title = clean_text(item.get('title'))
    url = urljoin(SOURCE_URL, item.get('fullUrl') or '')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)

    try:
        start = datetime.fromtimestamp(item['startDate'] / 1000, TIME_ZONE)
    except (KeyError, TypeError, ValueError, OSError):
        return None

    if not title or not venue or not city or not url.startswith(('http://', 'https://')):
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_event_skipped',
            level='warning',
            url=url or CALENDAR_URL,
            has_title=bool(title),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    description = clean_text(item.get('body')) or clean_text(item.get('excerpt')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
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
    offset = None
    seen_offsets = set()
    items_by_id = {}

    while True:
        params = {'format': 'json'}
        if offset is not None:
            params['offset'] = offset
        response = session.get(CALENDAR_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in payload.get('upcoming', []) + payload.get('past', []):
            item_id = item.get('id') or item.get('fullUrl')
            if item_id:
                items_by_id[item_id] = item

        pagination = payload.get('pagination') or {}
        next_offset = pagination.get('nextPageOffset')
        if not pagination.get('nextPage') or next_offset is None or next_offset in seen_offsets:
            break
        seen_offsets.add(next_offset)
        offset = next_offset

    records = [record for item in items_by_id.values() if (record := parse_item(item))]
    if not records:
        log_message(
            'No valid concert events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda record: (record['date'], record['time_from'], record['title']))


class OkChamberSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='okchambersymphony_com',
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
    OkChamberSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
