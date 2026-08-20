import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.venturamusicfestival.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Ventura Music Festival'
TIME_ZONE = ZoneInfo('America/Los_Angeles')

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', unescape(str(value)).replace('\xa0', ' ')).strip()


def html_text(value):
    if not value:
        return None
    soup = BeautifulSoup(unescape(value), 'html.parser')
    for element in soup.select('style, script, img, iframe'):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    lines = [clean_text(line) for line in text.splitlines()]
    text = '\n'.join(line for line in lines if line)
    return text or None


def parse_city(location):
    address_line = clean_text((location or {}).get('addressLine2'))
    if address_line:
        city = clean_text(address_line.split(',', 1)[0])
        if city:
            return city
    return None


def resolve_location(item, description):
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = parse_city(location)

    # This event is entered as TBA in Squarespace, but its first-party body
    # explicitly identifies both the Historic Pierpont Inn and Ventura.
    if venue.lower() == 'to be announced' and description:
        if 'Historic Pierpont Inn' in description or 'Pierpont Inn' in description:
            return 'The Historic Pierpont Inn', 'Ventura'
        return None

    if not venue:
        return None
    if not city:
        # The festival calendar is based in Ventura. Ojai and other touring
        # locations have explicit addressLine2 values in the feed.
        city = 'Ventura'
    return venue, city


def parse_item(item):
    title = clean_text(item.get('title'))
    path = clean_text(item.get('fullUrl'))
    start_ms = item.get('startDate')
    if not title or not path or not isinstance(start_ms, (int, float)):
        return None

    try:
        start = datetime.fromtimestamp(start_ms / 1000, tz=TIME_ZONE)
    except (OverflowError, OSError, ValueError):
        return None

    description = html_text(item.get('body')) or html_text(item.get('excerpt'))
    location = resolve_location(item, description)
    if not location:
        return None
    venue, city = location

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


def fetch_page(session, offset=None):
    params = {'format': 'json'}
    if offset is not None:
        params['offset'] = offset
    response = session.get(EVENTS_URL, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    skipped_count = 0
    offset = None
    seen_offsets = set()

    while True:
        payload = fetch_page(session, offset)
        items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
        for item in items:
            record = parse_item(item)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        pagination = payload.get('pagination') or {}
        next_offset = pagination.get('nextPageOffset')
        if not pagination.get('nextPage') or next_offset is None:
            break
        if next_offset in seen_offsets:
            raise ValueError(f'Repeated Squarespace pagination offset: {next_offset}')
        seen_offsets.add(next_offset)
        offset = next_offset

    if skipped_count:
        log_message(
            'Skipped event entries missing required date or location data',
            event='crawler_records_skipped',
            level='warning',
            url=EVENTS_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No event entries found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class VenturaMusicFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='venturamusicfestival_org',
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
    VenturaMusicFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
