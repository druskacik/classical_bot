import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wcfsymphony.org/'
SOURCE = 'Waterloo-Cedar Falls Symphony'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
PERFORMANCES_CATEGORY_ID = 9

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
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw or '>' in raw
        else raw
    )
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def is_recording_only(event):
    title = clean_text(event.get('title')).lower()
    description = clean_text(event.get('description')).lower()
    if 'live from the archive' in title or title.startswith('digital concert:'):
        return True
    return any(
        phrase in description
        for phrase in (
            'recorded audio from past performances',
            'special stream of an imaginary symphony',
            'free and on-demand presentation',
            'series launches digitally',
        )
    )


def make_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = event.get('start_date_details') or {}
    venue_data = event.get('venue') or {}
    if not isinstance(venue_data, dict):
        return None

    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    try:
        event_date = date(
            int(start.get('year')),
            int(start.get('month')),
            int(start.get('day')),
        ).isoformat()
    except (TypeError, ValueError):
        return None

    if not title or not url or not venue or not city or is_recording_only(event):
        return None

    time_from = None
    if not event.get('all_day'):
        try:
            time_from = f"{int(start.get('hour')):02d}:{int(start.get('minutes')):02d}"
        except (TypeError, ValueError):
            time_from = None

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


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        params = {
            'categories': PERFORMANCES_CATEGORY_ID,
            'start_date': '1900-01-01',
            'end_date': '2100-12-31',
            'per_page': 50,
            'page': page,
        }
        response = session.get(EVENTS_API, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []
        records.extend(record for event in events if (record := make_record(event)))

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    log_message(
        'Scraped performance feed',
        event='crawler_scrape_completed',
        url=EVENTS_API,
        record_count=len(records),
    )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class WcfSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wcfsymphony_org',
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
        return get_concerts()


def main():
    WcfSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
