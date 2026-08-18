from datetime import date
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.longbaysymphony.com/'
SOURCE = 'Long Bay Symphony'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

# The only API item before 2015 is a golf event whose stored 2000 date is a
# placeholder; the site's genuine event archive begins in 2015.
ARCHIVE_START = '2014-01-01 00:00:00'
ARCHIVE_END = '2100-12-31 23:59:59'


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(event):
    if event.get('all_day'):
        return None
    details = event.get('start_date_details') or {}
    hour = str(details.get('hour', '')).zfill(2)
    minute = str(details.get('minutes', '')).zfill(2)
    if re.fullmatch(r'\d{2}', hour) and re.fullmatch(r'\d{2}', minute):
        if 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59:
            return f'{hour}:{minute}'
    return None


def parse_event(event):
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        venue_data = {}

    title = clean_text(event.get('title'))
    event_date = parse_date(event.get('start_date'))
    url = clean_text(event.get('url'))
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    if not all((title, event_date, url, venue, city)):
        return None

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(event),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LongBaySymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='longbaysymphony_com',
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
        records = []
        page = 1
        while True:
            response = requests.get(
                API_URL,
                params={
                    'per_page': 50,
                    'page': page,
                    'start_date': ARCHIVE_START,
                    'end_date': ARCHIVE_END,
                    'status': 'publish',
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            events = payload.get('events', [])
            if not isinstance(events, list):
                raise ValueError('Long Bay Symphony API returned invalid events data')

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Long Bay Symphony event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(event.get('url')),
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, or city is missing',
                    )

            total_pages = int(payload.get('total_pages') or 1)
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    LongBaySymphonyComCrawler().run()


if __name__ == '__main__':
    main()
