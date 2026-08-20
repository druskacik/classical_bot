from datetime import date, datetime, timedelta

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://calendar.usm.edu/'
SOURCE = 'The University of Southern Mississippi Events Calendar'
API_URL = f'{SOURCE_URL}api/2/events'
CONCERTS_AND_PERFORMANCES_ID = 138074
ARCHIVE_START = date(2024, 1, 1)
PAGE_SIZE = 100

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

CAMPUS_CITIES = {
    'Hattiesburg Campus': 'Hattiesburg',
    'Gulf Park Campus': 'Long Beach',
}


def clean_text(value):
    if not isinstance(value, str):
        return ''
    return ' '.join(value.replace('\xa0', ' ').split())


def date_windows(start, end):
    cursor = start
    while cursor <= end:
        window_end = min(date(cursor.year, 12, 31), end)
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def event_city(event):
    geo = event.get('geo') or {}
    city = clean_text(geo.get('city'))
    if city:
        return city

    filters = event.get('filters') or {}
    campuses = filters.get('event_campus') or []
    inferred = {
        CAMPUS_CITIES[item.get('name')]
        for item in campuses
        if item.get('name') in CAMPUS_CITIES
    }
    return inferred.pop() if len(inferred) == 1 else ''


def event_records(event):
    title = clean_text(event.get('title'))
    venue = clean_text(event.get('location_name'))
    city = event_city(event)
    url = clean_text(event.get('localist_url'))
    description = clean_text(event.get('description_text')) or None

    if not title or not venue or venue.lower() == 'various locations' or not city or not url:
        return []

    records = []
    for wrapped_instance in event.get('event_instances') or []:
        instance = wrapped_instance.get('event_instance') or {}
        start = instance.get('start')
        try:
            parsed_start = datetime.fromisoformat(start)
        except (TypeError, ValueError):
            continue

        records.append({
            'title': title,
            'date': parsed_start.date().isoformat(),
            'url': url,
            'time_from': None if instance.get('all_day') else parsed_start.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None, today=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    today = today or date.today()
    end = today + timedelta(days=730)
    records = []

    for window_start, window_end in date_windows(ARCHIVE_START, end):
        page = 1
        while True:
            params = {
                'type': CONCERTS_AND_PERFORMANCES_ID,
                'start': window_start.isoformat(),
                'end': window_end.isoformat(),
                'pp': PAGE_SIZE,
                'page': page,
            }
            response = session.get(API_URL, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()

            for wrapped_event in payload.get('events') or []:
                event = wrapped_event.get('event') or {}
                records.extend(event_records(event))

            page_data = payload.get('page') or {}
            if page >= int(page_data.get('total') or 1):
                break
            page += 1

    unique = {
        (item['url'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['url']))
    if not result:
        log_message(
            'No candidate concert events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return result


class UsmEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='usm_edu',
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
    UsmEduCrawler().run()


if __name__ == '__main__':
    main()
