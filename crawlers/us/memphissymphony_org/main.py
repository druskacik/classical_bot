import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://memphissymphony.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Memphis Symphony Orchestra'
TIME_ZONE = ZoneInfo('America/Chicago')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, offset=None):
    params = {'format': 'json'}
    if offset is not None:
        params['offset'] = offset
    response = session.get(CALENDAR_URL, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def city_from_location(location):
    address_line = clean_text(location.get('addressLine2'))
    if address_line:
        # Squarespace stores this as "City, ST, ZIP" (occasionally without
        # the second comma). The locality is always the first component.
        city = address_line.split(',', 1)[0].strip()
        if city:
            return city

    # This is the MSO's own, Memphis-area calendar. A missing address on an
    # otherwise named venue is therefore safely local; explicit touring
    # locations retain their supplied city above.
    return 'Memphis'


def event_record(item):
    title = clean_text(item.get('title'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    start_timestamp = item.get('startDate')
    url_path = item.get('fullUrl') or (
        f"/calendar/{item.get('urlId')}" if item.get('urlId') else ''
    )
    url = urljoin(SOURCE_URL, url_path)

    if not title or not venue or not start_timestamp or not url_path:
        return None
    try:
        start = datetime.fromtimestamp(start_timestamp / 1000, TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None

    description_parts = [
        clean_text(item.get('body')),
        clean_text(item.get('excerpt')),
    ]
    description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part))
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city_from_location(location),
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items_by_id = {}
    offset = None
    seen_offsets = set()

    while True:
        data = get_json(session, offset)
        for item in data.get('upcoming', []) + data.get('past', []):
            item_id = item.get('id')
            if item_id:
                items_by_id[item_id] = item

        pagination = data.get('pagination') or {}
        next_offset = pagination.get('nextPageOffset') if pagination.get('nextPage') else None
        if next_offset is None or next_offset in seen_offsets:
            break
        seen_offsets.add(next_offset)
        offset = next_offset

    records = []
    for item in items_by_id.values():
        record = event_record(item)
        if record:
            records.append(record)
        else:
            log_message(
                'Skipping Memphis Symphony calendar item with incomplete event data',
                event='crawler_item_skipped',
                level='warning',
                url=urljoin(SOURCE_URL, item.get('fullUrl') or ''),
            )

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'], record['title']),
    )


class MemphisSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='memphissymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    return MemphisSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
