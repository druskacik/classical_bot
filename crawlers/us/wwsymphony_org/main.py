import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wwsymphony.org/'
SOURCE = 'Walla Walla Symphony'
EVENTS_URL = f'{SOURCE_URL}events'
TIMEZONE = ZoneInfo('America/Los_Angeles')

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for unwanted in soup(['script', 'style', 'noscript']):
        unwanted.decompose()
    text = html.unescape(soup.get_text('\n', strip=True))
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(dict.fromkeys(line for line in lines if line))


def city_from_location(location):
    address = location.get('addressLine2') or ''
    city = re.split(r',\s*(?:[A-Z]{2}\b|\d{5}\b)', address, maxsplit=1)[0].strip(' ,')
    return city or None


def parse_event(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    start_timestamp = item.get('startDate')
    if not all((title, path, venue, city, start_timestamp)):
        return None

    try:
        start = datetime.fromtimestamp(start_timestamp / 1000, TIMEZONE)
    except (TypeError, ValueError, OSError):
        return None

    description_parts = [
        clean_text(item.get('excerpt')),
        clean_text(item.get('body')),
    ]
    description = '\n'.join(dict.fromkeys(part for part in description_parts if part)) or None
    url = requests.compat.urljoin(SOURCE_URL, path)
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


class WwSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wwsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        seen_ids = set()
        offset = None

        while True:
            params = {'format': 'json'}
            if offset is not None:
                params['offset'] = offset
            try:
                response = session.get(EVENTS_URL, params=params, timeout=60)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Walla Walla Symphony events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if 'response' in locals() else EVENTS_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            page_items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
            for item in page_items:
                item_id = item.get('id') or item.get('fullUrl')
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                record = parse_event(item)
                if record:
                    records.append(record)

            pagination = payload.get('pagination') or {}
            next_offset = pagination.get('nextPageOffset')
            if not pagination.get('nextPage') or next_offset is None or next_offset == offset:
                break
            offset = next_offset

        return records


def main():
    return WwSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
