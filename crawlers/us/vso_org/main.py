import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.vso.org/'
SOURCE = 'Vermont Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

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
    raw = str(value)
    if '<' in raw:
        soup = BeautifulSoup(raw, 'html.parser')
        for node in soup(['script', 'style']):
            node.decompose()
        raw = soup.get_text('\n', strip=True)
    raw = html.unescape(raw).replace('\xa0', ' ').replace('\u200b', '')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in raw.splitlines()]
    return '\n'.join(line for line in lines if line)


def parse_start_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def record_from_event(event):
    starts_at = parse_start_date(event.get('start_date'))
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') or {}
    if not isinstance(venue_data, dict):
        return None
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    if not all((starts_at, title, url, venue, city)):
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '2000-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events')
        if not isinstance(events, list):
            raise ValueError('VSO events API returned an unexpected response')

        for event in events:
            record = record_from_event(event)
            if record:
                records.append(record)

        total_pages = payload.get('total_pages')
        if not isinstance(total_pages, int):
            raise ValueError('VSO events API response omitted pagination metadata')
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No parseable VSO events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class VsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='vso_org',
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
        return scrape_events()


def main():
    VsoOrgCrawler().run()


if __name__ == '__main__':
    main()
