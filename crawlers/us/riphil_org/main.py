import re
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.riphil.org/'
CALENDAR_URL = 'https://www.riphil.org/events'
SOURCE = 'Rhode Island Philharmonic Orchestra & Music School'
API_URL = 'https://core.service.elfsight.com/p/boot/'
WIDGET_ID = '26195b86-cb48-40d9-a600-579079d902e8'

# These are the complete first-party event-type choices exposed by the calendar.
# Requiring one of them also excludes three uncategorized "Example Event" test rows.
IN_SCOPE_EVENT_TYPES = {
    'TACO Classical',
    'Amica Rush Hour',
    'Open Rehearsal',
    'Special Events',
    'Music School Events',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_html(value):
    if not value:
        return None
    soup = BeautifulSoup(unescape(value), 'html.parser')
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def parse_city(location):
    combined = f"{location.get('name', '')} {location.get('address', '')}"
    for city in (
        'East Providence',
        'Central Falls',
        'Narragansett',
        'Providence',
        'Scituate',
        'Bristol',
        'Newport',
    ):
        if re.search(rf'\b{re.escape(city)}\b', combined, re.IGNORECASE):
            return city
    return None


def parse_event(event, event_types, locations):
    category_names = {
        event_types[event_type_id]
        for event_type_id in event.get('eventType', [])
        if event_type_id in event_types
    }
    if not category_names.intersection(IN_SCOPE_EVENT_TYPES):
        return None

    title = re.sub(r'\s+', ' ', event.get('name', '')).strip()
    event_id = event.get('id', '').strip()
    start = event.get('start') or {}
    event_date = start.get('date', '')
    try:
        event_date = date.fromisoformat(event_date).isoformat()
    except (TypeError, ValueError):
        return None

    location_ids = event.get('location') or []
    location = next((locations.get(location_id) for location_id in location_ids if locations.get(location_id)), None)
    if not title or not event_id or not location:
        return None

    venue = re.sub(r'\s+', ' ', location.get('name', '')).strip()
    city = parse_city(location)
    if not venue or not city:
        return None

    time_from = start.get('time')
    if time_from and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from):
        time_from = None

    return {
        'title': title,
        'date': event_date,
        'url': f'{CALENDAR_URL}#calendar-{WIDGET_ID}-event-{event_id}',
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_html(event.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class RiphilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='riphil_org',
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
        try:
            response = requests.get(
                API_URL,
                params={'page': CALENDAR_URL, 'w': WIDGET_ID},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            settings = payload['data']['widgets'][WIDGET_ID]['data']['settings']
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            log_message(
                'Failed to fetch Rhode Island Philharmonic events',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        event_types = {
            item['id']: item['name']
            for item in settings.get('eventTypes', [])
            if item.get('id') and item.get('name')
        }
        locations = {
            item['id']: item
            for item in settings.get('locations', [])
            if item.get('id')
        }
        records = [
            record
            for event in settings.get('events', [])
            if (record := parse_event(event, event_types, locations))
        ]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    RiphilOrgCrawler().run()


if __name__ == '__main__':
    main()
