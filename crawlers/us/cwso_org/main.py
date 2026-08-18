import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cwso.org/'
EVENTS_API_URL = 'https://cwso.org/wp-json/tribe/events/v1/events'
SOURCE = 'Central Wisconsin Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://cwso.org/events/',
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
        return None, None
    return parsed.date().isoformat(), None if all_day else parsed.strftime('%H:%M')


def event_to_record(event):
    venue_data = event.get('venue') or {}
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    event_date, time_from = parse_start(event.get('start_date'), event.get('all_day', False))

    if not all((title, event_date, url, venue, city)):
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url or EVENTS_API_URL,
            event_id=event.get('id'),
        )
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

    while True:
        response = session.get(
            EVENTS_API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '2000-01-01',
                'end_date': '2099-12-31',
                'status': 'publish',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []

        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not events:
            break
        page += 1

    log_message(
        'Concert candidate records parsed',
        event='crawler_parse_completed',
        record_count=len(records),
        url=EVENTS_API_URL,
    )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class CwsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cwso_org',
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
    CwsoOrgCrawler().run()


if __name__ == '__main__':
    main()
