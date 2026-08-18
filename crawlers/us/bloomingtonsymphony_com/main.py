import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bloomingtonsymphony.com/'
SOURCE = 'Bloomington Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36'
    ),
    'Upgrade-Insecure-Requests': '1',
    'sec-ch-ua': '"Chromium";v="151", "Not=A?Brand";v="99"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
}


def clean_html(value):
    if not value:
        return None
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def normalize_time(hour, minute, meridiem):
    hour = int(hour)
    if meridiem.lower().startswith('p') and hour != 12:
        hour += 12
    elif meridiem.lower().startswith('a') and hour == 12:
        hour = 0
    return f'{hour:02d}:{int(minute or 0):02d}'


def time_from_description(description):
    if not description:
        return None

    shared_meridiem = re.search(
        r'\b(\d{1,2})(?::(\d{2}))?\s*(?:and|&|or)\s*'
        r'\d{1,2}(?::\d{2})?\s*([ap])\.?m\.?\b',
        description,
        re.IGNORECASE,
    )
    if shared_meridiem:
        return normalize_time(*shared_meridiem.groups())

    match = re.search(
        r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b',
        description,
        re.IGNORECASE,
    )
    return normalize_time(*match.groups()) if match else None


def parse_event(event):
    venue_data = event.get('venue') or {}
    venue = clean_html(venue_data.get('venue'))
    city = clean_html(venue_data.get('city'))
    if not city and venue and 'bloomington' in venue.lower():
        city = 'Bloomington'

    title = clean_html(event.get('title'))
    url = event.get('url')
    start_date = event.get('start_date')
    if not title or not url or not venue or not city or not start_date:
        return None

    try:
        start = datetime.strptime(start_date, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    description = clean_html(event.get('description'))
    time_from = None
    if not event.get('all_day'):
        time_from = start.strftime('%H:%M')
    else:
        time_from = time_from_description(description)

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
    }


class BloomingtonSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bloomingtonsymphony_com',
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
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'per_page': 50,
            'start_date': '2000-01-01',
            'end_date': '2099-12-31',
            'page': 1,
        }
        records = []

        while True:
            try:
                response = session.get(API_URL, params=params, timeout=60)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Bloomington Symphony events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=params['page'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = int(payload.get('total_pages') or 1)
            if params['page'] >= total_pages:
                break
            params['page'] += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    BloomingtonSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
