import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.petersburgsymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Petersburg Symphony Orchestra'
DEFAULT_CITY = 'Petersburg'
DEFAULT_VENUE = 'Petersburg High School Auditorium'

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
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_datetime(timestamp):
    if not isinstance(timestamp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1000, ZoneInfo('America/New_York'))
    except (OSError, OverflowError, ValueError):
        return None


def resolve_location(event, description):
    location = event.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    address = ' '.join(
        clean_text(location.get(key))
        for key in ('addressLine1', 'addressLine2', 'addressCountry')
    )

    if not venue and 'petersburg high school auditorium' in description.lower():
        venue = DEFAULT_VENUE
        address = f'{address} Petersburg Virginia'

    if not venue or venue.lower() in {'tbd', 'to be determined'}:
        return None, None

    location_text = f'{venue} {address}'.lower()
    if 'hopewell' in location_text:
        city = 'Hopewell'
    elif 'petersburg' in location_text:
        city = DEFAULT_CITY
    else:
        return None, None
    return venue, city


def make_record(event):
    title = clean_text(event.get('title'))
    description = clean_text(event.get('body') or event.get('excerpt')) or None
    start = event_datetime(event.get('startDate'))
    url_path = clean_text(event.get('fullUrl')) or (
        f'/events/{event["urlId"]}' if event.get('urlId') else ''
    )
    url = urljoin(SOURCE_URL, url_path)
    venue, city = resolve_location(event, description or '')

    if not title or not start or not url_path or not venue or not city:
        return None

    # A season pass is a sales/overview entry rather than a performance.
    if 'season pass' in title.lower():
        return None

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


def get_concerts():
    response = requests.get(
        EVENTS_URL,
        params={'format': 'json'},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    events = (payload.get('upcoming') or []) + (payload.get('past') or [])

    records = []
    for event in events:
        record = make_record(event)
        if record:
            records.append(record)

    log_message(
        'Parsed first-party event feed',
        event='crawler_feed_parsed',
        url=response.url,
        record_count=len(records),
    )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class PetersburgSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='petersburgsymphony_org',
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
        return get_concerts()


def main():
    PetersburgSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
