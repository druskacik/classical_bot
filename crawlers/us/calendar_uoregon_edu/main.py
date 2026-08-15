from datetime import date
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://calendar.uoregon.edu/'
API_URL = f'{SOURCE_URL}api/2/events'
SOURCE = 'University of Oregon Events Calendar'

# Localist's Music, Concert, Dance, and Performance/Theater event types.  The
# feed is intentionally sent to potential_event: each type also contains
# non-classical events, while their union covers classical concerts, opera,
# dance, crossover, and potentially qualifying musical theatre.
EVENT_TYPE_IDS = (14468, 22507, 14471, 14470)
FIRST_ARCHIVE_YEAR = 2008
PAGE_SIZE = 100

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def city_from_event(event):
    geo = event.get('geo') or {}
    city = clean_text(geo.get('city'))
    if city:
        return city

    address = clean_text(event.get('address'))
    match = re.search(r',\s*([^,]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?$', address)
    if match:
        return clean_text(match.group(1))
    return ''


def clean_text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def record_from_event(event):
    title = clean_text(event.get('title'))
    venue = clean_text(event.get('location_name'))
    city = city_from_event(event)
    url = clean_text(event.get('localist_url'))
    description = clean_text(event.get('description_text')) or None

    instances = event.get('event_instances') or []
    if not title or not venue or venue.upper() == 'TBD' or not city or not url or not instances:
        return []

    records = []
    for wrapped_instance in instances:
        instance = wrapped_instance.get('event_instance') or {}
        start = clean_text(instance.get('start'))
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T.*', start):
            continue
        try:
            event_date = date.fromisoformat(start[:10]).isoformat()
        except ValueError:
            continue

        time_from = None if instance.get('all_day') else start[11:16]
        if time_from and not re.fullmatch(r'\d{2}:\d{2}', time_from):
            time_from = None

        records.append({
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
        })
    return records


def fetch_type_year(session, event_type_id, year):
    records = []
    page = 1
    while True:
        params = {
            'type': event_type_id,
            'start': f'{year}-01-01',
            'end': f'{year}-12-31',
            'pp': PAGE_SIZE,
            'page': page,
        }
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()

        event_wrappers = payload.get('events') or []
        for wrapped_event in event_wrappers:
            records.extend(record_from_event(wrapped_event.get('event') or {}))

        pagination = payload.get('page') or {}
        current_page = pagination.get('current', page)
        total_pages = pagination.get('total', current_page)
        if not event_wrappers or current_page >= total_pages:
            break
        page += 1
    return records


def scrape_concerts(session=None):
    session = session or build_session()
    records_by_key = {}
    final_year = date.today().year + 2

    for year in range(FIRST_ARCHIVE_YEAR, final_year + 1):
        for event_type_id in EVENT_TYPE_IDS:
            for record in fetch_type_year(session, event_type_id, year):
                key = (
                    record['url'],
                    record['date'],
                    record['time_from'],
                    record['venue'],
                )
                records_by_key[key] = record

    records = sorted(
        records_by_key.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )
    log_message(
        'University of Oregon calendar scrape completed',
        event='crawler_scrape_completed',
        url=API_URL,
        record_count=len(records),
    )
    return records


class CalendarUoregonEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='calendar_uoregon_edu',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CalendarUoregonEduCrawler().run()


if __name__ == '__main__':
    main()
