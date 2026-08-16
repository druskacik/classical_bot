import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://northcountrychamberplayers.org/'
SOURCE = 'North Country Chamber Players'
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
    text = re.sub(r'\[/?et_pb_[^\]]*\]', ' ', text)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    start_value = event.get('start_date')
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    # This venue's API record omits its city, but it is the named Rocks Estate
    # property in Bethlehem, New Hampshire.
    if venue_data.get('slug') == 'rocks-estate' and not city:
        city = 'Bethlehem'

    if not title or not url or not start_value or not venue or not city:
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
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class NorthCountryChamberPlayersOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='northcountrychamberplayers_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'per_page': 50,
            'start_date': '2000-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
            'page': 1,
        }
        records = []

        while True:
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch North Country Chamber Players events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=params['page'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events')
            if not isinstance(events, list):
                raise ValueError('Events API response does not contain an events list')

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = payload.get('total_pages', 1)
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
    NorthCountryChamberPlayersOrgCrawler().run()


if __name__ == '__main__':
    main()
