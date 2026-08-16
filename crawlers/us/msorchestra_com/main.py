import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://msorchestra.com/'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Mississippi Symphony Orchestra'

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
    value = str(value)
    if '<' in value and '>' in value:
        text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    else:
        text = unescape(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = event.get('start_date') or ''
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    if city == 'Pearls':
        city = 'Pearl'

    if not title or not url or not venue or not city or len(start) < 10:
        return None

    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'\b(\d{2}):(\d{2})(?::\d{2})?\b', start)
    time_from = None
    if not event.get('all_day') and time_match:
        time_from = f'{time_match.group(1)}:{time_match.group(2)}'

    description = clean_text(event.get('description')) or None
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


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    url = EVENTS_API
    params = {
        'start_date': '1900-01-01',
        'end_date': '2100-12-31',
        'per_page': 50,
        'page': 1,
    }
    records = []

    while url:
        try:
            response = session.get(url, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to retrieve events page',
                event='crawler_page_failed',
                level='error',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        for event in payload.get('events') or []:
            record = parse_event(event)
            if record:
                records.append(record)

        url = payload.get('next_rest_url')
        params = None

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class MsorchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='msorchestra_com',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MsorchestraComCrawler().run()


if __name__ == '__main__':
    main()
