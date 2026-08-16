import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://syracuseorchestra.org/'
SOURCE = 'The Syracuse Orchestra'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
ARCHIVE_START = '2000-01-01'
ARCHIVE_END = '2100-12-31'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_payload(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def iter_events(session):
    url = EVENTS_API
    params = {
        'per_page': 50,
        'start_date': ARCHIVE_START,
        'end_date': ARCHIVE_END,
        'status': 'publish',
    }
    while url:
        payload = get_payload(session, url, params=params)
        yield from payload.get('events') or []
        url = payload.get('next_rest_url')
        params = None


def make_record(event):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    start = event.get('start_date') or ''
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2}', start)

    if not title or not url or not venue or not city or not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None if event.get('all_day') else f'{match.group(2)}:{match.group(3)}'
    description = clean_text(event.get('description') or event.get('excerpt')) or None
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
    records = []
    skipped = 0

    for event in iter_events(session):
        record = make_record(event)
        if record:
            records.append(record)
        else:
            skipped += 1

    if skipped:
        log_message(
            'Skipped events without required date or location fields',
            event='crawler_items_skipped',
            level='warning',
            skipped_count=skipped,
        )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class ExperienceSymphoriaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='experiencesymphoria_org',
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
        return get_concerts()


def main():
    ExperienceSymphoriaOrgCrawler().run()


if __name__ == '__main__':
    main()
