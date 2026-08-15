import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://brevardsymphony.com/'
SOURCE = 'Brevard Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
FUNDRAISER_CATEGORY_ID = 109

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
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(event):
    value = event.get('start_date')
    if not value:
        return None, None
    try:
        start = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None, None
    time_from = None if event.get('all_day') else start.strftime('%H:%M')
    return start.date().isoformat(), time_from


def is_fundraiser(event):
    return any(category.get('id') == FUNDRAISER_CATEGORY_ID for category in event.get('categories', []))


def event_to_record(event):
    if is_fundraiser(event):
        return None

    event_date, time_from = parse_start(event)
    venue_data = event.get('venue') or {}
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
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


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    page = 1
    records = []
    skipped_count = 0

    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '2000-01-01 00:00:00',
                'status': 'publish',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events', [])

        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    if skipped_count:
        log_message(
            'Skipped non-concert or incomplete events',
            event='crawler_events_skipped',
            url=API_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class BrevardSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brevardsymphony_com',
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
    BrevardSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
