import html
import re
from datetime import datetime

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://hollandsymphony.org/'
SOURCE = 'Holland Symphony Orchestra'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n')
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


def parse_start(value, all_day=False):
    if not value:
        return None, None
    try:
        start = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return start.date().isoformat(), None if all_day else start.strftime('%H:%M')


def parse_event(event):
    event_date, time_from = parse_start(event.get('start_date'), event.get('all_day', False))
    venue_data = event.get('venue') or {}
    if not isinstance(venue_data, dict):
        venue_data = {}

    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    description = clean_text(event.get('description')) or None

    if not all((title, event_date, url, venue, city)):
        return None
    if not url.startswith(('https://', 'http://')):
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


class HollandsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hollandsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        records = []
        page = 1
        total_pages = 1

        try:
            while page <= total_pages:
                response = session.get(
                    EVENTS_API_URL,
                    params={
                        'per_page': 50,
                        'page': page,
                        'start_date': '2000-01-01 00:00:00',
                        'end_date': '2100-12-31 23:59:59',
                        'status': 'publish',
                    },
                    timeout=45,
                    # The site serves an incomplete certificate chain to the
                    # production Python image, although browsers accept it.
                    verify=False,
                )
                response.raise_for_status()
                payload = response.json()
                total_pages = int(payload.get('total_pages') or 1)

                for event in payload.get('events') or []:
                    record = parse_event(event)
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipping Holland Symphony event with incomplete required fields',
                            event='crawler_record_skipped',
                            level='warning',
                            url=event.get('url'),
                        )
                page += 1
        except (requests.RequestException, ValueError, TypeError) as error:
            log_message(
                'Failed to fetch Holland Symphony events API',
                event='crawler_api_request_failed',
                level='error',
                url=EVENTS_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        if not records:
            log_message(
                'No Holland Symphony events found',
                event='crawler_empty_listing',
                level='warning',
                url=EVENTS_API_URL,
                record_count=0,
            )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    HollandsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
