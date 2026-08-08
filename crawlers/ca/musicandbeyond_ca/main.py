import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musicandbeyond.ca/'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Music and Beyond'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-CA,en;q=0.9,fr-CA;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_events(session):
    # The Events Calendar API otherwise defaults to upcoming events. An early
    # start date also retrieves any past events the publisher keeps public.
    params = {
        'start_date': '2000-01-01 00:00:00',
        'per_page': 50,
        'page': 1,
    }
    events = []
    while True:
        response = session.get(EVENTS_API, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('events') or [])

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1
    return events


def make_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    start = event.get('start_date') or ''
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):\d{2}', start)
    if not title or not url or not venue or not city or not match:
        return None

    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    country = clean_text(venue_data.get('country')).lower()
    if country and country not in {'canada', 'ca'}:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'country_code': 'CA',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for event in get_events(session):
        record = make_record(event)
        if record:
            records.append(record)
        else:
            log_message(
                'Skipped event with incomplete or unsupported location data',
                event='crawler_item_skipped',
                level='warning',
                url=clean_text(event.get('url')),
            )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class MusicAndBeyondCaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicandbeyond_ca',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
        # The calendar also publishes non-concert fundraisers such as a wine
        # auction, so all entries need classification before direct insertion.
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
        return get_concerts()


def main():
    MusicAndBeyondCaCrawler().run()


if __name__ == '__main__':
    main()
