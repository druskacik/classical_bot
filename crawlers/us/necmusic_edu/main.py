import calendar
import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://necmusic.edu/'
CALENDAR_URL = 'https://www.trumba.com/calendars/nec.json'
SOURCE = 'New England Conservatory'
COUNTRY_CODE = 'US'
FIRST_ARCHIVE_YEAR = 2023

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

CITY_NAMES = ('Boston', 'Cambridge', 'Saugus', 'Rockport', 'Hancock', 'Malden', 'Belmont')
HOME_VENUE_MARKERS = (
    'jordan', 'williams', 'burnes', 'brown hall', 'pierce', 'keller',
    'plimpton shattuck', 'st. botolph', 'saint botolph', 'student life',
    'slpc', 'splc', 'bower stage', 'center for ceb', 'cultural equity',
    'ceps office', 'elfers commons', 'prevost room', 'carriage house violins',
    'blumenthal library', 'jh ', 'sb ',
)


def clean_text(value, separator=' '):
    if not value:
        return ''
    text = html.unescape(str(value))
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text(separator)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    if separator == '\n':
        lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
        return '\n'.join(line for line in lines if line)
    return re.sub(r'\s+', ' ', text).strip()


def custom_fields(event):
    return {
        field.get('label'): field.get('value')
        for field in event.get('customFields') or []
        if field.get('label')
    }


def archive_ranges():
    final_year = datetime.now().year + 2
    for year in range(FIRST_ARCHIVE_YEAR, final_year + 1):
        for start_month in (1, 4, 7, 10):
            end_month = start_month + 2
            last_day = calendar.monthrange(year, end_month)[1]
            yield f'{year}{start_month:02d}01', f'{year}{end_month:02d}{last_day:02d}'


def fetch_events(session):
    events = {}
    for start_date, end_date in archive_ranges():
        try:
            response = session.get(
                CALENDAR_URL,
                params={'startdate': start_date, 'enddate': end_date},
                timeout=60,
            )
            response.raise_for_status()
            batch = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'NEC calendar request failed',
                event='crawler_listing_failed',
                level='warning',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for item in batch:
            event_id = item.get('eventID')
            if event_id is not None:
                events[event_id] = item
    return list(events.values())


def resolve_location(raw_location):
    location = clean_text(raw_location, separator='\n')
    if not location or location.casefold() in {'n/a', 'virtual'}:
        return None, None

    one_line = clean_text(location)
    city = next((name for name in CITY_NAMES if re.search(rf'\b{name}\b', one_line, re.I)), None)
    if city == 'Hancock':
        city = 'Hancock'
    elif city == 'Rockport':
        city = 'Rockport'
    elif re.search(r'\bHyde Park\b', one_line, re.I):
        city = 'Boston'
    elif 'Distler Performance Hall' in one_line:
        city = 'Medford'
    elif not city and any(marker in one_line.casefold() for marker in HOME_VENUE_MARKERS):
        city = 'Boston'
    if not city:
        return None, None

    venue = location.split('\n', 1)[0]
    venue = re.split(r',\s*\d+\s', venue, maxsplit=1)[0].strip(' ,')
    if not venue:
        return None, None
    return venue, city


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('permaLinkUrl'))
    venue, city = resolve_location(event.get('location'))
    try:
        start = datetime.fromisoformat(event.get('startDateTime', ''))
    except (TypeError, ValueError):
        return None
    if not title or not url or not venue or not city:
        return None

    fields = custom_fields(event)
    description_parts = [clean_text(event.get('description'), separator='\n')]
    for label in ('Artist(s)', 'Program'):
        value = clean_text(fields.get(label), separator='\n')
        if value:
            description_parts.append(f'{label}:\n{value}')
    description = '\n\n'.join(part for part in description_parts if part)

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('allDay') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class NecmusicEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='necmusic_edu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = [record for event in fetch_events(session) if (record := parse_event(event))]
        if not records:
            log_message(
                'No valid NEC events found',
                event='crawler_empty_listing',
                level='warning',
                url=CALENDAR_URL,
                record_count=0,
            )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    NecmusicEduCrawler().run()


if __name__ == '__main__':
    main()
