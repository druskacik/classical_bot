import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://columbussymphony.com/'
SOURCE = 'Columbus Symphony'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

PARAMS = {
    'per_page': 50,
    'start_date': '2000-01-01 00:00:00',
    'end_date': '2100-12-31 23:59:59',
}


def clean_html(value):
    if not value:
        return ''
    raw_value = str(value)
    if '<' in raw_value and '>' in raw_value:
        text = BeautifulSoup(raw_value, 'html.parser').get_text('\n', strip=True)
    else:
        text = raw_value
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def parse_event(event):
    title = clean_html(event.get('title'))
    url = clean_html(event.get('url'))
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None

    venue = clean_html(venue_data.get('venue'))
    city = clean_html(venue_data.get('city'))
    if not title or not url.startswith(('https://', 'http://')) or not venue or not city:
        return None

    try:
        start = datetime.strptime(event.get('start_date', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    description = clean_html(event.get('description')) or None
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


class ColumbussymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='columbussymphony_com',
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
        session = make_session()
        next_url = EVENTS_API_URL
        params = PARAMS
        records = []

        try:
            while next_url:
                response = session.get(next_url, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()

                events = payload.get('events')
                if not isinstance(events, list):
                    raise ValueError('Columbus Symphony API returned no events list')

                for event in events:
                    record = parse_event(event)
                    if record:
                        records.append(record)

                next_url = payload.get('next_rest_url')
                params = None
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Columbus Symphony events',
                event='crawler_fetch_failed',
                level='error',
                url=next_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ColumbussymphonyComCrawler().run()


if __name__ == '__main__':
    main()
