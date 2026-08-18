import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hendersonvillesymphony.org/'
SOURCE = 'Hendersonville Symphony Orchestra'
TIME_ZONE = ZoneInfo('America/New_York')

COLLECTION_URLS = (
    urljoin(SOURCE_URL, 'hso-classics-i'),
    urljoin(SOURCE_URL, '5th-annual-divertimentos-dressage'),
)

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
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup.select('script, style, .sqs-block-button, .sqs-simple-like'):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def local_datetime(milliseconds):
    if not isinstance(milliseconds, (int, float)):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, TIME_ZONE)


def parse_city(location):
    address_line = (location.get('addressLine2') or '').strip()
    if address_line:
        city = address_line.split(',')[0].strip()
        if city:
            return city
    return None


def parse_event(item):
    title = html.unescape((item.get('title') or '')).strip()
    path = (item.get('fullUrl') or '').strip()
    start = local_datetime(item.get('startDate'))
    location = item.get('location') or {}
    venue = html.unescape((location.get('addressTitle') or '')).strip()
    city = parse_city(location)
    if not title or not path or start is None or not venue or not city:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(item.get('body') or item.get('excerpt')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HendersonvilleSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hendersonvillesymphony_org',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []

        for collection_url in COLLECTION_URLS:
            try:
                response = session.get(
                    collection_url,
                    params={'format': 'json'},
                    timeout=45,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Hendersonville Symphony event collection',
                    event='crawler_fetch_failed',
                    level='error',
                    url=collection_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
                record = parse_event(item)
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    HendersonvilleSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
