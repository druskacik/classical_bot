import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://schubert.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Schubert Club'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

# Older venue records in the first-party API sometimes omit their city. These
# venue-specific defaults cover the affected physical locations without applying
# the Schubert Club's home city to touring performances.
VENUE_CITY_DEFAULTS = {
    'Landmark Center Courtroom 317': 'Saint Paul',
    'Park Square Theater': 'Saint Paul',
    "Schell's Stage at Schilling Amphitheater": 'New Ulm',
}


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value))
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value, all_day=False):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return '', None
    return parsed.date().isoformat(), None if all_day else parsed.strftime('%H:%M')


def normalize_city(value):
    city = clean_text(value)
    if city.lower() in {'st. paul', 'st paul'}:
        return 'Saint Paul'
    return city


def event_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    event_date, time_from = parse_start(event.get('start_date'), event.get('all_day', False))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = normalize_city(venue_data.get('city'))
    if not city:
        city = VENUE_CITY_DEFAULTS.get(venue, '')

    if not all((title, event_date, url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
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
    total_pages = 1

    while page <= total_pages:
        response = session.get(
            EVENTS_API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '2000-01-01',
                'end_date': '2100-01-01',
                'status': 'publish',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        total_pages = int(payload.get('total_pages') or 1)

        for event in payload.get('events', []):
            record = event_record(event)
            if record:
                records.append(record)
        page += 1

    if not records:
        log_message(
            'No valid event records found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class SchubertOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='schubert_org',
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
    SchubertOrgCrawler().run()


if __name__ == '__main__':
    main()
