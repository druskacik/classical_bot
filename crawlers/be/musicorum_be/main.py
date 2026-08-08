import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musicorum.be/'
SOURCE = 'Festival Musicorum'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'fr-BE,fr;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = unescape(str(value))
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    start = event.get('start_date') or ''

    match = re.fullmatch(
        r'(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?', start
    )
    if not title or not url or not venue or not city or not match:
        return None

    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'country_code': 'BE',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_events(session):
    params = {
        'start_date': '2000-01-01',
        'end_date': '2100-01-01',
        'per_page': 50,
        'page': 1,
        'status': 'publish',
    }
    events = []

    while True:
        response = session.get(EVENTS_API, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        page_events = payload.get('events') or []
        events.extend(page_events)

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1

    return events


class MusicorumBeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicorum_be',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = fetch_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Musicorum events',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MusicorumBeCrawler().run()


if __name__ == '__main__':
    main()
