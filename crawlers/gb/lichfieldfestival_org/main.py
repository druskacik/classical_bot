import html
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lichfieldfestival.org/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Lichfield Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style, noscript, iframe'):
        node.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def get_json(session, params, attempts=5):
    error = None
    for attempt in range(attempts):
        try:
            response = session.get(API_URL, params=params, timeout=60)
            if response.status_code in {401, 403, 429, 500, 502, 503, 504}:
                response.raise_for_status()
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as caught:
            error = caught
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise error


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    start = event.get('start_date')
    if not all((title, url, venue, city, start)):
        return None

    try:
        start_value = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': start_value.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start_value.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    skipped_count = 0
    page = 1

    while True:
        params = {
            'per_page': 50,
            'page': page,
            'start_date': '1900-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
            'status': 'publish',
        }
        data = get_json(session, params)
        events = data.get('events') or []
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        total_pages = int(data.get('total_pages') or 1)
        if page >= total_pages or not events:
            break
        page += 1

    if skipped_count:
        log_message(
            'Skipped Lichfield Festival events with incomplete details',
            event='crawler_items_skipped',
            level='warning',
            skipped_count=skipped_count,
        )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class LichfieldFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lichfieldfestival_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
    LichfieldFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
