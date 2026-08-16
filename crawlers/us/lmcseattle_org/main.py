from datetime import datetime
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lmcseattle.org/'
SOURCE = 'Ladies Musical Club of Seattle'
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
        return None
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def parse_date_time(event):
    details = event.get('start_date_details') or {}
    try:
        event_date = datetime(
            int(details['year']), int(details['month']), int(details['day'])
        ).date().isoformat()
    except (KeyError, TypeError, ValueError):
        return None, None

    if event.get('all_day'):
        return event_date, None
    try:
        event_time = f"{int(details['hour']):02d}:{int(details['minutes']):02d}"
        datetime.strptime(event_time, '%H:%M')
    except (KeyError, TypeError, ValueError):
        event_time = None
    return event_date, event_time


def event_to_record(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    event_date, event_time = parse_date_time(event)

    if not all((title, event_date, url, venue, city)):
        return None
    if not str(url).startswith(('http://', 'https://')):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')),
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'start_date': '1900-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
                'per_page': 50,
                'page': page,
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
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No valid event records found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class LmcSeattleOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lmcseattle_org',
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
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return scrape_events()


def main():
    LmcSeattleOrgCrawler().run()


if __name__ == '__main__':
    main()
