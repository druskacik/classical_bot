import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.amherstsymphony.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'events?format=json')
SOURCE = 'Amherst Symphony Orchestra'
COUNTRY_CODE = 'US'
DEFAULT_CITY = 'Williamsville'
DEFAULT_VENUE = 'Williamsville South High School'
LOCAL_TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

NON_PERFORMANCE_TITLES = re.compile(r'\b(?:rehearsal|board meeting)\b', re.I)


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    text = html.unescape(soup.get_text('\n', strip=True)).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_pages(session):
    url = EVENTS_URL
    seen_urls = set()
    while url and url not in seen_urls:
        seen_urls.add(url)
        response = session.get(url, timeout=45)
        response.raise_for_status()
        payload = response.json()
        yield payload

        next_url = payload.get('pagination', {}).get('nextPageUrl')
        if not next_url:
            break
        separator = '&' if '?' in next_url else '?'
        url = urljoin(SOURCE_URL, f'{next_url}{separator}format=json')


def city_from_location(location):
    address_line = clean_text(location.get('addressLine2'))
    if not address_line:
        return ''
    return address_line.split(',', 1)[0].strip()


def venue_and_city(item, description):
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    title = clean_text(item.get('title'))

    if venue and city:
        return venue, city

    if title.lower() == 'summer concert' and 'Island Park' in description:
        return 'Island Park', DEFAULT_CITY

    if title.lower().startswith('september sounds & supper'):
        match = re.search(r'(Main-Transit Fire Hall).*?Williamsville', description, re.S | re.I)
        if match:
            return clean_text(match.group(1)), DEFAULT_CITY

    # The orchestra identifies this as its new concert home, and its calendar
    # records for the same season place named concerts there even when copied
    # records have an empty Squarespace location field.
    if 'concert' in title.lower():
        return DEFAULT_VENUE, DEFAULT_CITY

    return venue, city


def record_from_item(item):
    title = clean_text(item.get('title'))
    if not title or NON_PERFORMANCE_TITLES.search(title):
        return None

    start_ms = item.get('startDate')
    if not isinstance(start_ms, (int, float)):
        return None
    try:
        start = datetime.fromtimestamp(start_ms / 1000, tz=LOCAL_TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return None

    description = clean_text(item.get('body')) or None
    venue, city = venue_and_city(item, description or '')
    path = item.get('fullUrl')
    url = urljoin(SOURCE_URL, path) if path else ''
    if not venue or not city or not url.startswith(('http://', 'https://')):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    seen_items = set()

    for payload in api_pages(session):
        for item in [*payload.get('upcoming', []), *payload.get('past', [])]:
            item_id = item.get('id') or item.get('fullUrl')
            if not item_id or item_id in seen_items:
                continue
            seen_items.add(item_id)
            record = record_from_item(item)
            if record:
                records.append(record)

    if not records:
        log_message(
            'No candidate concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class AmherstSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='amherstsymphony_com',
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
    AmherstSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
