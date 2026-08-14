import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.om-macau.org/'
SOURCE = 'Macao Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
ARCHIVE_START = '2000-01-01'
PAGE_SIZE = 50

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_html(value):
    if not value:
        return ''
    value = re.sub(r'\[(?:/?vc_|/?cq)[^\]]*\]', ' ', str(value), flags=re.IGNORECASE)
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def parse_timestamp(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def parse_event(event):
    start = parse_timestamp(event.get('start_date'))
    end = parse_timestamp(event.get('end_date'))
    venue_data = event.get('venue') or {}
    title = clean_html(event.get('title'))
    venue = clean_html(venue_data.get('venue'))
    url = event.get('url')

    # The API occasionally contains draft-like occurrences without a venue.
    # A city is defensible for this Macao-based orchestra, but a hall is not.
    if not all((start, title, venue, url)):
        return None

    all_day = bool(event.get('all_day'))
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if all_day else start.time().strftime('%H:%M'),
        'time_to': None if all_day or end is None else end.time().strftime('%H:%M'),
        'venue': venue,
        'city': 'Macao',
        'country_code': 'MO',
        'description': clean_html(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OmMacauOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='om_macau_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='MO',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'time_to', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            params = {
                'per_page': PAGE_SIZE,
                'page': page,
                'start_date': ARCHIVE_START,
            }
            try:
                response = session.get(API_URL, params=params, timeout=60)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Macao Orchestra events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events') or []
            for event in events:
                record = parse_event(event)
                if record is not None:
                    records.append(record)

            if not payload.get('next_rest_url'):
                break
            page += 1

        log_message(
            'Parsed Macao Orchestra event archive',
            event='crawler_parse_completed',
            record_count=len(records),
            page_count=page,
        )
        return records


def main():
    OmMacauOrgCrawler().run()


if __name__ == '__main__':
    main()
