import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://newhavensymphony.org/'
SOURCE = 'New Haven Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for node in soup(['script', 'style', 'noscript']):
        node.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(event):
    value = event.get('start_date', '')
    try:
        start = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    time_from = None if event.get('all_day') else start.strftime('%H:%M')
    return start.date().isoformat(), time_from


def parse_event(event):
    title = clean_html(event.get('title'))
    event_date, time_from = parse_start(event)
    url = event.get('url', '').strip()
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None
    venue = clean_html(venue_data.get('venue'))
    city = clean_html(venue_data.get('city'))
    if not all((title, event_date, url, venue, city)):
        return None

    description = clean_html(event.get('description')) or None
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


class NewHavenSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='newhavensymphony_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1
        total_pages = 1
        skipped_count = 0

        while page <= total_pages:
            params = {
                'start_date': '1900-01-01',
                'per_page': 50,
                'page': page,
            }
            try:
                response = session.get(API_URL, params=params, timeout=60)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch New Haven Symphony events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events')
            if not isinstance(events, list):
                raise ValueError('New Haven Symphony API returned no event list')
            try:
                total_pages = max(1, int(payload.get('total_pages', 1)))
            except (TypeError, ValueError) as error:
                raise ValueError('New Haven Symphony API returned invalid pagination') from error

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    skipped_count += 1
            page += 1

        if skipped_count:
            log_message(
                'Skipped New Haven Symphony events missing required fields',
                event='crawler_records_skipped',
                level='warning',
                record_count=skipped_count,
                url=API_URL,
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    NewHavenSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
