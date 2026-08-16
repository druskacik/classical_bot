import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bvsymphony.org/'
SOURCE = 'Brandywine Valley Symphony'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

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
    text = html.unescape(str(value))
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start_date = clean_text(event.get('start_date'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    try:
        start = datetime.strptime(start_date, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None

    if not all((title, url, venue, city)) or not url.startswith(('http://', 'https://')):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
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
            EVENTS_API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '1900-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []

        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete required fields',
                    event='crawler_record_skipped',
                    level='warning',
                    url=clean_text(event.get('url')),
                    event_id=event.get('id'),
                )

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No parseable events returned by the events API',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class KennettSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kennettsymphony_org',
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
        return scrape_concerts()


def main():
    KennettSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
