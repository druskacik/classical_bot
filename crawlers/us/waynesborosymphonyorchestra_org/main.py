from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://waynesborosymphonyorchestra.org/'
SOURCE = 'Waynesboro Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

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
        return ''
    return ' '.join(BeautifulSoup(unescape(str(value)), 'html.parser').get_text(' ', strip=True).split())


def parse_datetime(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def event_to_record(event):
    start = parse_datetime(event.get('start_date'))
    venue_data = event.get('venue') or {}
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    if not start or not title or not url.startswith(('http://', 'https://')) or not venue or not city:
        log_message(
            'Skipping event with missing required fields',
            event='crawler_invalid_event',
            level='warning',
            url=url or API_URL,
            event_id=event.get('id'),
        )
        return None

    description = clean_text(event.get('description')) or clean_text(event.get('excerpt')) or None
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


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'page': page,
                'per_page': 50,
                'start_date': '1900-01-01 00:00:00',
                'status': 'publish',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()

        for event in payload.get('events', []):
            record = event_to_record(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class WaynesboroSymphonyOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='waynesborosymphonyorchestra_org',
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
        return scrape_concerts()


def main():
    WaynesboroSymphonyOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
