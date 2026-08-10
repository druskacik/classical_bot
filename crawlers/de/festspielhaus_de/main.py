import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festspielhaus.de/de'
PROGRAM_URL = f'{SOURCE_URL}/programm'
EVENTS_API = f'{SOURCE_URL}/api/events/'
SOURCE = 'Festspielhaus Baden-Baden'
CITY = 'Baden-Baden'

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


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def listing_events(session):
    # The calendar API retains the site's available past season. An early
    # starting date returns that archive together with all future events.
    url = EVENTS_API
    params = {'date': '2000-01-01', 'page_size': 200}
    events = []
    while url:
        payload = get_json(session, url, params=params)
        events.extend(payload.get('results') or [])
        url = payload.get('next')
        params = None
    return events


def event_url(event):
    absolute_url = clean_text(event.get('absolute_url'))
    if absolute_url:
        return urljoin(SOURCE_URL, absolute_url)

    event_id = event.get('id')
    slug = event.get('slug') or {}
    slug = slug.get('de') if isinstance(slug, dict) else slug
    if not event_id or not slug:
        return ''
    return f'{PROGRAM_URL}/{slug}/{event_id}'


def venue_name(detail):
    venue_data = detail.get('venue') or {}
    room_data = detail.get('room') or {}
    building = clean_text(
        venue_data.get('description_short') or venue_data.get('name')
    )
    room = clean_text(
        room_data.get('description_short') or room_data.get('name')
    )

    if building and room:
        if building.casefold() in room.casefold():
            return room
        if room.casefold() in building.casefold():
            return building
        return f'{building}, {room}'
    return building or room


def detail_description(detail, fallback=None):
    parts = []
    for key in ('description_short', 'important_information'):
        value = clean_text(detail.get(key))
        if value and value not in parts:
            parts.append(value)

    works = []
    for work in detail.get('works') or []:
        composer = clean_text(work.get('description_short'))
        name = clean_text(work.get('name'))
        notes = clean_text(work.get('description_long'))
        heading = ': '.join(value for value in (composer, name) if value)
        item = '\n'.join(value for value in (heading, notes) if value)
        if item:
            works.append(item)
    if works:
        parts.append('Programm\n' + '\n\n'.join(works))

    return '\n\n'.join(parts) or clean_text(fallback) or None


def make_record(event, detail=None):
    detail = detail or event
    title = clean_text(detail.get('name') or event.get('name'))
    subtitle = clean_text(detail.get('subtitle') or event.get('subtitle'))
    if subtitle and subtitle.casefold() not in title.casefold():
        title = f'{title} – {subtitle}'

    start = detail.get('date_start') or event.get('date_start')
    url = event_url(detail) or event_url(event)
    venue = venue_name(detail) or venue_name(event)
    if not title or not start or not url or not venue:
        return None
    try:
        start_at = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None

    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': 'DE',
        'description': detail_description(detail, event.get('description_short')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_json, session, f'{EVENTS_API}{event["id"]}/'): event
            for event in events
            if event.get('id')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                record = make_record(event, future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event_url(event),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                record = make_record(event)
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FestspielhausDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festspielhaus_de',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FestspielhausDeCrawler().run()


if __name__ == '__main__':
    main()
