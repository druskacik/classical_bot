import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://artofelan.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Art of Elan'
TIME_ZONE = ZoneInfo('America/Los_Angeles')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = re.sub(r'[ \t]+', ' ', text.replace('\xa0', ' '))
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_location(location):
    address = clean_text((location or {}).get('addressLine2'))
    if not address:
        return ''
    return address.split(',', 1)[0].strip()


def local_datetime(milliseconds):
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None


def record_from_item(item):
    title = clean_text(item.get('title'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    starts_at = local_datetime(item.get('startDate'))
    url_id = str(item.get('urlId') or '').strip('/')

    # Squarespace uses an empty/default New York map record when no location was
    # supplied. A future placeholder is likewise not a defensible venue.
    if not title or not starts_at or not url_id or not city or not venue:
        return None
    if venue.casefold() in {'venue tbd', 'tbd', 'art of elan'}:
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(LISTING_URL + '/', url_id),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(item.get('body') or item.get('excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    next_url = f'{LISTING_URL}?format=json'
    seen_pages = set()
    seen_items = set()
    records = []

    while next_url and next_url not in seen_pages:
        seen_pages.add(next_url)
        response = session.get(next_url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
            item_id = item.get('id') or item.get('urlId')
            if not item_id or item_id in seen_items:
                continue
            seen_items.add(item_id)
            record = record_from_item(item)
            if record:
                records.append(record)

        page_url = (payload.get('pagination') or {}).get('nextPageUrl')
        if page_url:
            separator = '&' if '?' in page_url else '?'
            next_url = urljoin(SOURCE_URL, page_url) + separator + 'format=json'
        else:
            next_url = None

    if not records:
        log_message(
            'No usable concert events found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ArtofelanOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='artofelan_org',
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
    ArtofelanOrgCrawler().run()


if __name__ == '__main__':
    main()
