import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.alteoper.de/de/'
EVENTS_API = f'{SOURCE_URL}api/events/'
SOURCE = 'Alte Oper Frankfurt'
VENUE = 'Alte Oper Frankfurt'
CITY = 'Frankfurt am Main'

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
    # The public calendar retains an archive beginning in 2000. Asking for
    # include_past makes the API return both that archive and future events.
    url = EVENTS_API
    params = {'include_past': 1, 'from_date': '2000-01-01'}
    events = []
    while url:
        payload = get_json(session, url, params=params)
        events.extend(payload.get('results') or [])
        url = payload.get('next')
        params = None
    return events


def event_url(event):
    event_id = event.get('id')
    slug = clean_text(event.get('slug'))
    if not event_id or not slug:
        return ''
    return f'{SOURCE_URL}programm/{slug}/{event_id}'


def detail_description(detail):
    parts = []
    for key in ('headline', 'subtitle', 'introduction', 'description'):
        value = clean_text(detail.get(key))
        if value and value not in parts:
            parts.append(value)

    programme = []
    for item in detail.get('composers') or []:
        composer = clean_text(
            ' '.join(filter(None, (item.get('first_name'), item.get('last_name'))))
        )
        works = clean_text(item.get('composition'))
        if composer and works:
            programme.append(f'{composer}\n{works}')
        elif composer or works:
            programme.append(composer or works)
    if programme:
        parts.append('Programm\n' + '\n\n'.join(programme))

    return '\n\n'.join(parts) or None


def make_record(event, detail=None):
    detail = detail or event
    title = clean_text(detail.get('title') or event.get('title'))
    url = event_url(detail) or event_url(event)
    room = clean_text(detail.get('room') or event.get('room'))
    start = detail.get('start_date') or event.get('start_date')
    if not title or not url or not room or not start:
        return None

    try:
        start_at = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None

    # Every room returned by this venue calendar is within the Alte Oper.
    # Preserve the room name while making the building explicit.
    venue = VENUE if room.lower() == VENUE.lower() else f'{VENUE}, {room}'
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': 'DE',
        'description': detail_description(detail),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=16) as executor:
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


class AlteoperDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='alteoper_de',
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
    AlteoperDeCrawler().run()


if __name__ == '__main__':
    main()
