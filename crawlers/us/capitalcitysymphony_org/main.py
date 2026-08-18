from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.capitalcitysymphony.org/'
SOURCE = 'Capital City Symphony'
CALENDAR_PATHS = ('upcoming-events', 'pastevents')
TIME_ZONE = ZoneInfo('America/New_York')
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
    soup = BeautifulSoup(str(value), 'html.parser')
    for element in soup(['script', 'style', 'iframe']):
        element.decompose()
    lines = [' '.join(line.split()) for line in soup.get_text('\n').splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def event_datetime(item):
    timestamp = item.get('startDate')
    if not isinstance(timestamp, (int, float)):
        timestamp = (item.get('structuredContent') or {}).get('startDate')
    if not isinstance(timestamp, (int, float)):
        return None, None
    try:
        value = datetime.fromtimestamp(timestamp / 1000, TIME_ZONE)
    except (OSError, OverflowError, ValueError):
        return None, None
    return value.date().isoformat(), value.strftime('%H:%M')


def event_city(location):
    address = clean_text(location.get('addressLine2'))
    if not address:
        return ''
    return address.split(',', 1)[0].strip()


def parse_event(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    url = urljoin(SOURCE_URL, path) if path else ''
    event_date, time_from = event_datetime(item)
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = event_city(location)
    description = clean_text(item.get('body') or item.get('excerpt')) or None

    if not all((title, event_date, url, venue, city)):
        return None
    return {
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


def fetch_collection(path):
    url = urljoin(SOURCE_URL, path)
    params = {'format': 'json'}
    items = []
    seen_offsets = set()

    while True:
        response = requests.get(url, params=params, headers=HEADERS, timeout=45)
        response.raise_for_status()
        payload = response.json()
        items.extend(payload.get('upcoming') or [])
        items.extend(payload.get('past') or [])

        pagination = payload.get('pagination') or {}
        offset = pagination.get('nextPageOffset')
        if not pagination.get('nextPage') or offset is None or offset in seen_offsets:
            break
        seen_offsets.add(offset)
        params['offset'] = offset

    return items


class CapitalCitySymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='capitalcitysymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        records = []
        seen_urls = set()
        for path in CALENDAR_PATHS:
            try:
                items = fetch_collection(path)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Capital City Symphony calendar',
                    event='crawler_page_failed',
                    level='warning',
                    url=urljoin(SOURCE_URL, path),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for item in items:
                record = parse_event(item)
                item_url = urljoin(SOURCE_URL, item.get('fullUrl') or '')
                if record and record['url'] not in seen_urls:
                    records.append(record)
                    seen_urls.add(record['url'])
                elif not record:
                    log_message(
                        'Skipped incomplete Capital City Symphony event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=item_url,
                        error_type='IncompleteEventData',
                        error_message='Required date, title, URL, venue, or city is missing',
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    CapitalCitySymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
