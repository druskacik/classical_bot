import html
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brso.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'event-calendar')
SOURCE = 'Baton Rouge Symphony Orchestra'
COUNTRY_CODE = 'US'
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
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_url(url):
    parsed = urlparse(urljoin(CALENDAR_URL, url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({'view': 'list', 'format': 'json'})
    return urlunparse(parsed._replace(query=urlencode(query)))


def local_datetime(timestamp_ms):
    try:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=LOCAL_TIMEZONE)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def city_from_location(location):
    line = clean_text(location.get('addressLine2'))
    return line.split(',', 1)[0].strip() if line else ''


def parse_item(item):
    title = clean_text(item.get('title'))
    start = local_datetime(item.get('startDate'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    path = item.get('fullUrl') or (
        f"/event-calendar/{item['urlId']}" if item.get('urlId') else ''
    )
    url = urljoin(SOURCE_URL, path)

    if not all((title, start, venue, city, path)):
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url or CALENDAR_URL,
            error_type='IncompleteEventData',
        )
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': clean_text(item.get('body') or item.get('excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    next_url = api_url(CALENDAR_URL)
    seen_pages = set()
    seen_items = set()
    records = []

    while next_url and next_url not in seen_pages:
        seen_pages.add(next_url)
        response = session.get(next_url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
            item_key = item.get('id') or item.get('fullUrl') or item.get('urlId')
            if not item_key or item_key in seen_items:
                continue
            seen_items.add(item_key)
            record = parse_item(item)
            if record:
                records.append(record)

        next_path = (payload.get('pagination') or {}).get('nextPageUrl')
        next_url = api_url(next_path) if next_path else None

    if not records:
        log_message(
            'No events found in calendar API',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BrsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    BrsoOrgCrawler().run()


if __name__ == '__main__':
    main()
