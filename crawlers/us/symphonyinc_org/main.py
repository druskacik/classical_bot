from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://symphonyinc.org/'
EVENTS_API_URL = f'{SOURCE_URL}?rest_route=/polyphony-events/v1/list'
SOURCE = 'Symphony in C'
COUNTRY_CODE = 'US'
LOCAL_TIMEZONE = ZoneInfo('America/New_York')
PAGE_SIZE = 100

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event_datetime(value):
    """The API stores event timestamps as UTC without an explicit offset."""
    try:
        utc_datetime = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None
    return utc_datetime.astimezone(LOCAL_TIMEZONE)


def event_to_record(event):
    meta = event.get('meta') or {}
    event_datetime = parse_event_datetime(meta.get('event_start_timestamp'))
    title = clean_text(event.get('title'))
    url = event.get('permalink')
    venue = clean_text(meta.get('event_location_name'))
    city = clean_text(meta.get('event_city'))

    if not event_datetime or not title or not url or not venue or not city:
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_invalid_event',
            level='warning',
            url=url or EVENTS_API_URL,
            event_id=event.get('ID'),
        )
        return None

    description = clean_text('\n'.join(event.get('content') or [])) or None
    return {
        'title': title,
        'date': event_datetime.date().isoformat(),
        'url': url,
        'time_from': event_datetime.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    offset = 0

    while True:
        response = session.get(
            EVENTS_API_URL,
            params={'offset': offset, 'count': PAGE_SIZE},
            timeout=60,
        )
        response.raise_for_status()
        events = response.json().get('events') or []

        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)

        if len(events) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if not records:
        log_message(
            'No events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['url']),
    )


class SymphonyIncOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='symphonyinc_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
        return scrape_events()


def main():
    SymphonyIncOrgCrawler().run()


if __name__ == '__main__':
    main()
