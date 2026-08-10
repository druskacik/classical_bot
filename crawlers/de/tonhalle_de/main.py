import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tonhalle.de/'
EVENTS_API = urljoin(SOURCE_URL, 'api/events')
SOURCE = 'Tonhalle Düsseldorf'
CITY = 'Düsseldorf'
DEFAULT_VENUE = 'Tonhalle Düsseldorf'
PAGE_SIZE = 500

# The public API treats zero as "no date filter". This timestamp (2000-01-01)
# requests the complete archive currently published by the site (from 2018).
ARCHIVE_START = 946684800
LOCAL_TIMEZONE = ZoneInfo('Europe/Berlin')

HEADERS = {
    'Accept': 'application/json, text/html;q=0.9, */*;q=0.8',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
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
    events = []
    offset = 0
    while True:
        payload = get_json(
            session,
            EVENTS_API,
            params={
                'type': 'dynamic',
                'size': PAGE_SIZE,
                'from': offset,
                'dateFrom': ARCHIVE_START,
            },
        )
        batch = payload.get('events') or []
        events.extend(batch)
        next_offset = payload.get('nextOffset')
        if next_offset is None or not batch:
            break
        offset = int(next_offset)
    return events


def detail_event(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    node = soup.select_one('script[data-page="app"][type="application/json"]')
    if not node or not node.string:
        raise ValueError('event page has no structured event payload')
    payload = json.loads(node.string)
    event = (payload.get('props') or {}).get('event')
    if not isinstance(event, dict):
        raise ValueError('structured event payload has no event')
    return event


def resolve_venue(event):
    for item in event.get('iconTextList') or []:
        if item.get('icon') == 'marker':
            venue = clean_text(item.get('text'))
            if venue:
                return venue
    # This is a venue calendar, and its events are held in the Tonhalle unless
    # a room is explicitly supplied. The site does not publish tour dates here.
    return DEFAULT_VENUE


def description_for(event):
    parts = []
    body = clean_text(event.get('description'))
    if body:
        parts.append(body)

    contributors = event.get('contributors') or {}
    programme = []
    for item in contributors.get('middle') or []:
        name = clean_text(item.get('text'))
        work = clean_text(item.get('info'))
        if name:
            programme.append(f'{name}: {work}' if work else name)
    if programme:
        parts.append('Programm\n' + '\n'.join(programme))

    return '\n\n'.join(parts) or None


def parse_performance(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
        local = parsed.astimezone(LOCAL_TIMEZONE)
        return local.date().isoformat(), local.strftime('%H:%M')
    except (AttributeError, TypeError, ValueError):
        return None


def records_for(event, fallback):
    title = clean_text(event.get('title') or fallback.get('title'))
    subtitle = clean_text(event.get('subTitle') or fallback.get('subTitle'))
    if subtitle and subtitle.casefold() not in title.casefold():
        title = f'{title} – {subtitle}'
    if not title:
        return []

    venue = resolve_venue(event)
    description = description_for(event)
    dates = event.get('dates') or [
        {'isoDateString': value, 'url': fallback.get('link')}
        for value in fallback.get('isoDateStrings') or []
    ]
    records = []
    for performance in dates:
        parsed = parse_performance(performance.get('isoDateString'))
        path = performance.get('url') or fallback.get('link')
        if not parsed or not path or not venue:
            continue
        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': urljoin(SOURCE_URL, path),
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'DE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {}
        for event in events:
            path = event.get('link')
            if path:
                url = urljoin(SOURCE_URL, path)
                futures[executor.submit(detail_event, session, url)] = (event, url)

        for future in as_completed(futures):
            fallback, url = futures[future]
            try:
                detail = future.result()
            except (json.JSONDecodeError, requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                detail = fallback
            records.extend(records_for(detail, fallback))

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class TonhalleDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tonhalle_de',
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
    TonhalleDeCrawler().run()


if __name__ == '__main__':
    main()
