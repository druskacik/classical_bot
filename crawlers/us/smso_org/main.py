from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://smso.org/'
SOURCE = 'Southwest Michigan Symphony Orchestra'
API_URL = 'https://smso.org/wp-json/tribe/events/v1/events'

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

# This archived item combines two separately advertised performances in one
# API object. The event description explicitly assigns Friday to the Mendel
# Center and Saturday to Shadowland Pavilion, while the structured venue only
# represents Saturday.
OCCURRENCE_OVERRIDES = {
    'https://smso.org/event/the-sound-of-music/': [
        ('2023-07-28', '18:30', 'The Mendel Center', 'Benton Harbor'),
        ('2023-07-29', '18:30', 'Shadowland Pavilion', 'St Joseph'),
    ],
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    return '\n'.join(line.strip() for line in text.splitlines() if line.strip())


def clean_title(value):
    text = clean_text(value)
    return text.split('\n', 1)[0].strip() if text else ''


def parse_event(event):
    title = clean_title(event.get('title'))
    url = str(event.get('url') or '').strip()
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None

    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    start_value = str(event.get('start_date') or '')
    try:
        start = datetime.strptime(start_value, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None

    if not title or not url or not venue or not city:
        return None

    all_day = bool(event.get('all_day'))
    time_from = None if all_day or start.strftime('%H:%M') == '00:00' else start.strftime('%H:%M')
    base_record = {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    overrides = OCCURRENCE_OVERRIDES.get(url)
    if not overrides:
        return [base_record]
    return [
        {
            **base_record,
            'date': event_date,
            'time_from': event_time,
            'venue': event_venue,
            'city': event_city,
        }
        for event_date, event_time, event_venue, event_city in overrides
    ]


def fetch_events(session):
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 50,
                'page': page,
                # The API accepts broad explicit bounds and otherwise defaults
                # to upcoming events only. Its date validator rejects 1970-01-01.
                'start_date': '1900-01-01 00:00:00',
                'end_date': '2037-12-31 23:59:59',
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []
        if not isinstance(events, list):
            raise ValueError('SMSO events API returned an invalid events collection')
        yield from events

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    try:
        for event in fetch_events(session):
            event_records = parse_event(event)
            if event_records:
                records.extend(event_records)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to scrape SMSO events API',
            event='crawler_failed',
            level='error',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    unique_records = {
        (
            record['title'], record['date'], record['time_from'],
            record['venue'], record['city'],
        ): record
        for record in records
    }
    return sorted(
        unique_records.values(),
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class SmsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='smso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    SmsoOrgCrawler().run()


if __name__ == '__main__':
    main()
