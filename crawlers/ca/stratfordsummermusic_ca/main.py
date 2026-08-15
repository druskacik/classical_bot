from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.stratfordsummermusic.ca/'
SOURCE = 'Stratford Summer Music'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
LOCAL_TIMEZONE = ZoneInfo('America/Toronto')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-CA,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    lines = [' '.join(unescape(line).split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def parse_timestamp(value):
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=LOCAL_TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return None


def extract_city(location):
    address_line = clean_text((location or {}).get('addressLine2'))
    if address_line:
        city = address_line.split(',', 1)[0].strip()
        if city:
            return city

    # The festival describes its programme as taking place at venues throughout
    # Stratford. Individual records still need a real venue before they are kept.
    return 'Stratford'


def make_record(item):
    title = clean_text(item.get('title'))
    starts_at = parse_timestamp(item.get('startDate'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = extract_city(location)
    path = clean_text(item.get('fullUrl'))
    url = urljoin(SOURCE_URL, path) if path else ''

    if not title or starts_at is None or not url or not venue or not city:
        return None

    description = clean_text(item.get('body')) or clean_text(item.get('excerpt')) or None
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'CA',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_feed(session, tag=None):
    records = []
    offset = None
    seen_offsets = set()

    while True:
        params = {'format': 'json'}
        if tag:
            params['tag'] = tag
        if offset is not None:
            params['offset'] = offset

        try:
            response = session.get(EVENTS_URL, params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch events feed',
                event='crawler_page_failed',
                level='error',
                url=EVENTS_URL,
                tag=tag,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records.extend(payload.get('upcoming') or [])
        records.extend(payload.get('past') or [])

        pagination = payload.get('pagination') or {}
        if not pagination.get('nextPage'):
            return records, payload

        next_offset = pagination.get('nextPageOffset')
        if next_offset is None or next_offset in seen_offsets:
            log_message(
                'Stopped events pagination with an invalid repeated offset',
                event='crawler_pagination_stopped',
                level='warning',
                url=EVENTS_URL,
                tag=tag,
            )
            return records, payload
        seen_offsets.add(next_offset)
        offset = next_offset


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    items, first_payload = fetch_feed(session)
    # Squarespace's unfiltered archive can omit tagged occurrences from deeper
    # pages. Query every published first-party tag as well, then merge by item ID.
    tags = first_payload.get('collection', {}).get('tags') or []
    for tag in tags:
        tagged_items, _ = fetch_feed(session, clean_text(tag))
        items.extend(tagged_items)

    unique_items = {}
    for item in items:
        item_id = item.get('id')
        if item_id:
            unique_items[item_id] = item

    unique_records = {}
    for item in unique_items.values():
        record = make_record(item)
        if record is None:
            log_message(
                'Skipped event with incomplete required fields',
                event='crawler_item_skipped',
                level='warning',
                url=urljoin(SOURCE_URL, clean_text(item.get('fullUrl'))),
            )
            continue
        key = (
            record['title'].casefold(),
            record['date'],
            record['time_from'],
            record['venue'].casefold(),
        )
        unique_records.setdefault(key, record)

    return sorted(
        unique_records.values(),
        key=lambda record: (
            record['date'], record['time_from'], record['title'], record['venue']
        ),
    )


class StratfordsummermusicCaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stratfordsummermusic_ca',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
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
        return get_concerts()


def main():
    StratfordsummermusicCaCrawler().run()


if __name__ == '__main__':
    main()
