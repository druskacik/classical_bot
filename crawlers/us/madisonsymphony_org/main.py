import html
import re
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://madisonsymphony.org/'
SOURCE = 'Madison Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

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
    value = html.unescape(html.unescape(str(value)))
    # Older event bodies contain Divi shortcodes around otherwise useful prose.
    value = re.sub(r'\[/?et_pb_[^\]]*\]', ' ', value, flags=re.I)
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_dates(event):
    try:
        start = date.fromisoformat(event['start_date'][:10])
        end = date.fromisoformat(event.get('end_date', event['start_date'])[:10])
    except (KeyError, TypeError, ValueError):
        return []

    if end < start:
        return []
    # The Events Calendar stores multi-performance weekends as inclusive ranges.
    # Do not turn a malformed season-long range into hundreds of occurrences.
    if (end - start).days > 14:
        return [start.isoformat()]
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]


def event_time(event):
    if event.get('all_day'):
        return None
    try:
        return datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S').strftime('%H:%M')
    except (KeyError, TypeError, ValueError):
        return None


def record_data(event):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    venue_data = event.get('venue') or {}
    if not isinstance(venue_data, dict):
        venue_data = {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    dates = event_dates(event)
    if not title or not url.startswith(('http://', 'https://')) or not venue or not city or not dates:
        return []

    description = clean_text(event.get('description')) or clean_text(event.get('excerpt')) or None
    time_from = event_time(event)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


def configured_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(
            max_retries=Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=(403, 429, 500, 502, 503, 504),
                allowed_methods=('GET',),
            )
        ),
    )
    return session


def scrape_concerts(session=None):
    owns_session = session is None
    session = session or configured_session()
    params = {
        'per_page': 50,
        'page': 1,
        'start_date': '1900-01-01',
        'end_date': '2100-12-31',
    }
    records = []

    while True:
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []
        for event in events:
            records.extend(record_data(event))

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1
        # Reusing Cloudflare's connection/cookie state causes later public API
        # pages to be rejected. A fresh polite request works consistently.
        if owns_session:
            session.close()
            session = configured_session()
        time.sleep(1)

    if not records:
        log_message(
            'No parseable calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class MadisonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='madisonsymphony_org',
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
    MadisonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
