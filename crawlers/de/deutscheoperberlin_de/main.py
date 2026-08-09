import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://deutscheoperberlin.de/de_DE/home'
CALENDAR_URL = 'https://deutscheoperberlin.de/de_DE/calendar'
EVENTS_API = 'https://deutscheoperberlin.de/de_DE/event.json'
SOURCE = 'Deutsche Oper Berlin'
HOME_CITY = 'Berlin'
HOME_COORDINATES = (13.30874, 52.51184)
HOME_VENUES = {'Großes Haus', 'Tischlerei', 'Foyer'}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, params):
    response = session.get(EVENTS_API, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def listing_events(session):
    # The public calendar API is the same endpoint used by the site's infinite
    # scroll. The backend clamps historical dates to its currently published
    # catalogue, so starting today returns every event it actually exposes.
    page = 1
    events = []
    while True:
        payload = get_json(
            session,
            {
                'current_page': 'calendar',
                'date_from': date.today().isoformat(),
                'location': 'alle',
                'category': 'alle',
                'status': '',
                'p': page,
            },
        )
        page_events = payload.get('EventOverview') or []
        events.extend(page_events)
        pager = payload.get('Pager') or {}
        if pager.get('IsLastPage') or not page_events:
            break
        page += 1
        if page > 200:
            raise RuntimeError('Calendar API exceeded 200 pages')
    return events


def event_date(event):
    year_match = re.search(r'(\d{4})', str(event.get('DateMonthYear') or ''))
    try:
        return date(
            int(year_match.group(1)),
            int(event.get('DateMonthNum')),
            int(event.get('DateDayNum')),
        ).isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def event_time(event):
    match = re.search(r'\b(\d{1,2}):(\d{2})\b', event.get('DateTimeStart') or '')
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def resolve_location(event):
    venue = clean_text(event.get('Location'))
    if not venue or venue.lower() in {'alle', 'diverse orte', 'verschiedene orte'}:
        return None, None

    city = clean_text(event.get('City'))
    if city:
        # CultureBase renders districts in square brackets, e.g.
        # "Berlin [ Charlottenburg ]". The database city is simply Berlin.
        city = clean_text(city.split('[', 1)[0])
        return venue, city or None

    # These are rooms of the Deutsche Oper itself. Some API entries omit the
    # city and contain slightly displaced map coordinates for the same room.
    if venue in HOME_VENUES:
        return venue, HOME_CITY

    try:
        longitude = float(event.get('Longitude'))
        latitude = float(event.get('Latitude'))
    except (TypeError, ValueError):
        return None, None
    if abs(longitude - HOME_COORDINATES[0]) < 0.001 and abs(latitude - HOME_COORDINATES[1]) < 0.001:
        return venue, HOME_CITY
    return None, None


def event_description(event):
    parts = []
    for key in ('ShortDescription', 'Description', 'OpusInfo'):
        value = clean_text(event.get(key))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def make_record(event):
    title = clean_text(event.get('Title'))
    slug = event.get('Slug') or ''
    url = urljoin(CALENDAR_URL, slug)
    date_value = event_date(event)
    venue, city = resolve_location(event)
    if not title or not slug or not date_value or not venue or not city:
        return None
    return {
        'title': title,
        'date': date_value,
        'url': url,
        'time_from': event_time(event),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': event_description(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records_by_id = {}
    skipped = 0
    for event in events:
        record = make_record(event)
        event_id = event.get('IdEventDate')
        if record and event_id is not None:
            records_by_id[event_id] = record
        elif record:
            records_by_id[record['url']] = record
        else:
            skipped += 1
    if skipped:
        log_message(
            'Skipped calendar events without a valid date or location',
            event='crawler_items_skipped',
            level='warning',
            record_count=skipped,
        )
    return sorted(
        records_by_id.values(),
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class DeutscheOperBerlinDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='deutscheoperberlin_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        return get_concerts()


def main():
    DeutscheOperBerlinDeCrawler().run()


if __name__ == '__main__':
    main()
