import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lpomusic.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Louisiana Philharmonic Orchestra'
# Squarespace renders these timestamps in the site's configured Pacific time
# zone (the detail pages confirm the resulting local dates and times).
TIME_ZONE = ZoneInfo('America/Los_Angeles')

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
    path = item.get('fullUrl') or ''
    event_url = urljoin(SOURCE_URL, path)

    if not all((title, start, path, venue, city)):
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
    url = f'{EVENTS_URL}?format=json'
    seen_pages = set()
    seen_events = set()
    records = []

    while url and url not in seen_pages:
        seen_pages.add(url)
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch LPO events',
                event='crawler_fetch_failed',
                level='error',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        for item in payload.get('upcoming', []) + payload.get('past', []):
            event_id = item.get('id') or item.get('fullUrl')
            if event_id in seen_events:
                continue
            record = record_from_item(item)
            if record:
                records.append(record)
                seen_events.add(event_id)

        pagination = payload.get('pagination') or {}
        next_path = pagination.get('nextPageUrl') if pagination.get('nextPage') else None
        if next_path:
            separator = '&' if '?' in next_path else '?'
            url = urljoin(SOURCE_URL, f'{next_path}{separator}format=json')
        else:
            url = None

    if not records:
        log_message(
            'No valid LPO events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['venue'], item['title']),
    )


class LpoMusicComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lpomusic_com',
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
    LpoMusicComCrawler().run()


if __name__ == '__main__':
    main()
