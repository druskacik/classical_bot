import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rockfordsymphony.com/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-and-events')
SOURCE = 'Rockford Symphony Orchestra'
TIME_ZONE = ZoneInfo('America/Chicago')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style, noscript, svg'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_city(location):
    address_line = (location or {}).get('addressLine2') or ''
    # Squarespace consistently emits "City, State, ZIP" for this US calendar.
    city = address_line.split(',', 1)[0].strip()
    if city and not re.search(r'\d', city):
        return city
    # A small number of older entries put the complete address in addressLine1.
    address_line = (location or {}).get('addressLine1') or ''
    match = re.search(r',\s*([^,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$', address_line)
    if match:
        return match.group(1).strip()
    return ''


def parse_timestamp(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000, TIME_ZONE)
    except (TypeError, ValueError, OverflowError):
        return None


def description_from_item(item):
    parts = []
    for field in ('excerpt', 'body'):
        text = clean_html(item.get(field))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def record_from_item(item):
    title = clean_html(item.get('title'))
    start = parse_timestamp(item.get('startDate'))
    location = item.get('location') or {}
    venue = clean_html(location.get('addressTitle'))
    city = parse_city(location)
    path = item.get('fullUrl')
    url = urljoin(SOURCE_URL, path or '')

    if not title or not start or not path or not venue or not city:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
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

    records = []
    seen_items = set()
    seen_offsets = set()
    offset = None

    while True:
        params = {'format': 'json'}
        if offset is not None:
            params['offset'] = offset

        response = session.get(LISTING_URL, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()

        for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
            item_id = item.get('id') or item.get('fullUrl')
            if not item_id or item_id in seen_items:
                continue
            seen_items.add(item_id)
            record = record_from_item(item)
            if record:
                records.append(record)

        pagination = payload.get('pagination') or {}
        next_offset = pagination.get('nextPageOffset')
        if not pagination.get('nextPage') or next_offset is None or next_offset in seen_offsets:
            break
        seen_offsets.add(next_offset)
        offset = next_offset

    if not records:
        log_message(
            'No valid event records found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class RockfordSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rockfordsymphony_com',
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
    RockfordSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
