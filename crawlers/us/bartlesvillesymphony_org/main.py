import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bartlesvillesymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events?format=json')
SOURCE = 'Bartlesville Symphony Orchestra'
DEFAULT_CITY = 'Bartlesville'
DEFAULT_VENUE = 'Bartlesville Community Center'
LOCAL_TIMEZONE = ZoneInfo('America/Chicago')

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


def local_datetime(milliseconds):
    try:
        return datetime.fromtimestamp(float(milliseconds) / 1000, tz=LOCAL_TIMEZONE)
    except (TypeError, ValueError, OSError):
        return None


def city_from_location(location):
    address = clean_text((location or {}).get('addressLine2'))
    if address:
        city = address.split(',', 1)[0].strip()
        if city:
            return city
    return DEFAULT_CITY


def record_from_item(item):
    title = clean_text(item.get('title'))
    start = local_datetime(item.get('startDate'))
    path = item.get('fullUrl') or item.get('urlId')
    if path and not str(path).startswith('/'):
        path = f'/events/{path}'
    url = urljoin(SOURCE_URL, path or '')

    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle')) or DEFAULT_VENUE
    city = city_from_location(location)
    if not title or not start or not path or not venue or not city:
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


def scrape_events(session=None):
    session = session or requests.Session()
    response = session.get(EVENTS_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    payload = response.json()

    items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
    records = []
    for item in items:
        record = record_from_item(item)
        if record:
            records.append(record)
        else:
            log_message(
                'Skipping event with incomplete required fields',
                event='crawler_event_skipped',
                level='warning',
                url=urljoin(SOURCE_URL, item.get('fullUrl') or ''),
                error_type='IncompleteEventData',
            )

    if not records:
        log_message(
            'No events found in collection',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BartlesvilleSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bartlesvillesymphony_org',
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
        return scrape_events()


def main():
    BartlesvilleSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
