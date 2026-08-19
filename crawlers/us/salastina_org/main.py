import html
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.salastina.org/'
COLLECTION_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Salastina'
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
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def json_url(url):
    parts = urlsplit(urljoin(SOURCE_URL, url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def city_from_location(location):
    address_line = clean_text((location or {}).get('addressLine2'))
    if not address_line:
        return ''

    # Squarespace normally emits "City, State, ZIP" here.  Keep only the city;
    # an address, state, or postal code is not an acceptable city fallback.
    city = address_line.split(',', 1)[0].strip()
    if city and not re.fullmatch(r'\d[\d -]*', city):
        return city
    return ''


def record_from_item(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    start_timestamp = item.get('startDate')

    if not title or not path or not venue or not city or not start_timestamp:
        return None

    try:
        start = datetime.fromtimestamp(float(start_timestamp) / 1000, TIME_ZONE)
    except (TypeError, ValueError, OSError, OverflowError):
        return None

    url = urljoin(SOURCE_URL, path)
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
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
    page_url = json_url(COLLECTION_URL)
    seen_pages = set()
    items = {}

    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
            item_id = item.get('id') or item.get('fullUrl')
            if item_id:
                items[item_id] = item

        next_page = (payload.get('pagination') or {}).get('nextPageUrl')
        page_url = json_url(next_page) if next_page else None

    records = []
    skipped_count = 0
    for item in items.values():
        record = record_from_item(item)
        if record:
            records.append(record)
        else:
            skipped_count += 1

    if skipped_count:
        log_message(
            'Skipped calendar items missing a required date or location field',
            event='crawler_items_skipped',
            level='warning',
            url=COLLECTION_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No parseable calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=COLLECTION_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['url']),
    )


class SalastinaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='salastina_org',
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
    SalastinaOrgCrawler().run()


if __name__ == '__main__':
    main()
