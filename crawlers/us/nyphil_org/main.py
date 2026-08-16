import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nyphil.org/'
SOURCE = 'New York Philharmonic'
API_URL = 'https://d1c3g0ihb82aph.cloudfront.net/Prod/events/9/2/none/live'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Referer': SOURCE_URL,
    'Content-Type': 'application/json',
    'Accept': 'application/json',
}

TOUR_LOCATIONS = {
    'Barcelona, Spain': ('Barcelona', 'ES'),
    'Madrid, Spain': ('Madrid', 'ES'),
    'Berlin, Germany': ('Berlin', 'DE'),
    'Hamburg, Germany': ('Hamburg', 'DE'),
    'Paris, France': ('Paris', 'FR'),
    'Vienna, Austria': ('Vienna', 'AT'),
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True) if '<' in raw else raw
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def location_from_venue(venue):
    for marker, location in TOUR_LOCATIONS.items():
        if marker.casefold() in venue.casefold():
            return location
    return 'New York', 'US'


def event_description(event):
    parts = []
    summary = clean_text(event.get('Summary'))
    if summary:
        parts.append(summary)

    programs = []
    for program in event.get('Programs') or []:
        composer = clean_text(program.get('Title'))
        work = clean_text(program.get('Details'))
        line = ': '.join(item for item in (composer, work) if item)
        if line and line not in programs:
            programs.append(line)
    if programs:
        parts.append('Program:\n' + '\n'.join(programs))

    people = []
    for person in event.get('People') or []:
        name = clean_text(person.get('DisplayName') or person.get('Name'))
        role = clean_text(person.get('Role') or person.get('Instrument'))
        line = ' — '.join(item for item in (name, role) if item)
        if line and line not in people:
            people.append(line)
    if people:
        parts.append('Artists:\n' + '\n'.join(people))

    return '\n\n'.join(parts) or None


def records_from_events(events):
    records = []
    for event in events:
        title = clean_text(event.get('StrippedTitle') or event.get('Title'))
        url = clean_text(event.get('EventLink'))
        venue = clean_text(event.get('Venue'))
        if not title or not url or not venue:
            continue

        city, country_code = location_from_venue(venue)
        description = event_description(event)
        for performance in event.get('Performances') or []:
            starts_at = parse_datetime(performance.get('Date'))
            if not starts_at:
                continue
            records.append({
                'title': title,
                'date': starts_at.date().isoformat(),
                'url': url,
                'time_from': starts_at.strftime('%H:%M'),
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']),
    )


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(API_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    events = response.json()
    if not isinstance(events, list):
        raise ValueError('NY Phil events API returned an unexpected response')

    records = records_from_events(events)
    if not records:
        log_message(
            'No NY Phil event performances found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return records


class NyphilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nyphil_org',
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
        return scrape_concerts()


def main():
    NyphilOrgCrawler().run()


if __name__ == '__main__':
    main()
