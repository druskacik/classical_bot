import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://greenlakefestival.org/'
SOURCE = 'Green Lake Festival of Music'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    start_value = event.get('start_date')

    if not title or not url or not venue or not city or not start_value:
        return None

    try:
        start = datetime.strptime(start_value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'description': description,
    }


class GreenLakeFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='greenlakefestival_org',
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
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            params = {
                'per_page': 50,
                'page': page,
                'start_date': '1900-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
            }
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Green Lake Festival events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = int(payload.get('total_pages') or 0)
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    GreenLakeFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
