import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil.rrule import rrulestr

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orkestras.pro/'
EVENTS_API = f'{SOURCE_URL}wp-json/wp/v2/mec-events'
SOURCE = 'Palangos orkestras'
COUNTRY_CODE = 'LT'
DEFAULT_CITY = 'Palanga'
VENUE_ALIASES = {
    'grafų tiškevičių al. 1, palanga': 'Palangos kurhauzo teatro salė',
}
LOCAL_TIMEZONE = ZoneInfo('Europe/Vilnius')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'lt-LT,lt;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def unfold_ical(text):
    return re.sub(r'\r?\n[ \t]', '', text)


def ical_value(text, name):
    match = re.search(rf'(?m)^{re.escape(name)}(?:;[^:]*)?:(.*)$', text)
    return match.group(1).strip() if match else ''


def parse_ical_datetime(value):
    if not value:
        return None
    try:
        if value.endswith('Z'):
            parsed = datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(
                tzinfo=ZoneInfo('UTC')
            )
            return parsed.astimezone(LOCAL_TIMEZONE)
        parsed = datetime.strptime(value, '%Y%m%dT%H%M%S')
        return parsed.replace(tzinfo=LOCAL_TIMEZONE)
    except ValueError:
        return None


def occurrences(ical_text):
    text = unfold_ical(ical_text)
    start = parse_ical_datetime(ical_value(text, 'DTSTART'))
    if not start:
        return []
    rule = ical_value(text, 'RRULE')
    if not rule:
        return [start]
    try:
        return list(rrulestr(rule, dtstart=start))
    except (TypeError, ValueError):
        return [start]


def list_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API,
            params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'desc'},
            timeout=45,
        )
        if response.status_code == 400 and events:
            break
        response.raise_for_status()
        batch = response.json()
        events.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return events


def fetch_event(session, event):
    event_id = event.get('id')
    if not event_id:
        return []
    response = session.get(
        SOURCE_URL, params={'method': 'ical', 'id': event_id}, timeout=45
    )
    response.raise_for_status()
    ical_text = response.text

    title = clean_text((event.get('title') or {}).get('rendered'))
    url = event.get('link') or ical_value(unfold_ical(ical_text), 'URL')
    venue = clean_text(ical_value(unfold_ical(ical_text), 'LOCATION'))
    venue = VENUE_ALIASES.get(venue.casefold(), venue)
    description = clean_text((event.get('content') or {}).get('rendered')) or None
    if not title or not url or not venue:
        return []

    records = []
    for start in occurrences(ical_text):
        records.append(
            {
                'title': title,
                'date': start.date().isoformat(),
                'url': url,
                'time_from': start.strftime('%H:%M'),
                'venue': venue,
                'city': DEFAULT_CITY,
                'country_code': COUNTRY_CODE,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = list_events(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_event, session, event): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=event.get('link'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'], record['title'], record['url']
        ),
    )


class OrkestrasProCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orkestras_pro',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
        return get_concerts()


def main():
    OrkestrasProCrawler().run()


if __name__ == '__main__':
    main()
