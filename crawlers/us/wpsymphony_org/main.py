import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wpsymphony.org/'
SOURCE = 'Western Piedmont Symphony'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

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
    text = unescape(str(value))
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    start = clean_text(event.get('start_date'))
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2}', start)
    if not title or not url or not venue or not city or not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None
    if not event.get('all_day'):
        time_from = f'{match.group(2)}:{match.group(3)}'

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': clean_text(event.get('description')) or None,
    }


def fetch_events(session):
    page = 1
    events = []
    while True:
        params = {
            'start_date': '2000-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
            'per_page': 50,
            'page': page,
            'status': 'publish',
        }
        response = session.get(EVENTS_API, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        page_events = payload.get('events') or []
        events.extend(page_events)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not page_events:
            return events
        page += 1


class WpSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wpsymphony_org',
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
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = fetch_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Western Piedmont Symphony events',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        skipped_count = 0
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        if skipped_count:
            log_message(
                'Skipped Western Piedmont Symphony entries missing required fields',
                event='crawler_items_skipped',
                level='warning',
                record_count=skipped_count,
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    WpSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
