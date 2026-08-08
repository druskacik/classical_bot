import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tso.ca/'
CALENDAR_URL = f'{SOURCE_URL}concerts-and-events/calendar'
EVENTS_API = 'https://d2brfggrg1ktvi.cloudfront.net/Prod/event-feed/18'
SOURCE = 'Toronto Symphony Orchestra'
CITY = 'Toronto'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-CA,en;q=0.9',
}

VENUE_ALIASES = {
    'Roy Thomson Hall': 'Roy Thomson Hall',
    'RTH Film & Orchestra': 'Roy Thomson Hall',
    'George Weston Recital Hall': 'George Weston Recital Hall',
    'GWRH 26': 'George Weston Recital Hall',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def get_json(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


def listing_description(event):
    items = []
    for item in event.get('program_items') or []:
        composer = clean_text(item.get('title'))
        work = clean_text(item.get('description'))
        line = ': '.join(part for part in (composer, work) if part)
        if line:
            items.append(line)
    return 'Program\n' + '\n'.join(items) if items else None


def detail_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    parts = []
    # The first prose panel on an event page is its editorial synopsis. Other
    # prose panels can contain generic visitor and subscription information.
    synopsis = soup.select_one('.content-blocks .content-panel__body.s-prose')
    if synopsis:
        text = clean_text(synopsis)
        if text:
            parts.append(text)

    programme = []
    for item in soup.select('.program-panel .program-item'):
        composer = clean_text(item.select_one('.program-item__title'))
        work = clean_text(item.select_one('.program-item__description'))
        caption = clean_text(item.select_one('.program-item__caption'))
        line = ': '.join(part for part in (composer, work) if part)
        if caption:
            line = f'{line} ({caption})' if line else caption
        if line:
            programme.append(line)
    if programme:
        parts.append('Program\n' + '\n'.join(programme))
    return '\n\n'.join(parts) or None


def resolve_venue(event):
    facilities = event.get('facilities') or []
    for facility in facilities:
        name = clean_text(facility.get('Title'))
        if name in VENUE_ALIASES:
            return VENUE_ALIASES[name]
    return None


def make_record(event, descriptions):
    title = clean_text(event.get('title'))
    url = event.get('link') or ''
    venue = resolve_venue(event)
    raw_date = event.get('date') or ''
    try:
        start = datetime.fromisoformat(raw_date)
    except (TypeError, ValueError):
        return None
    if not title or not url.startswith('http') or not venue:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': 'CA',
        'description': descriptions.get(url) or listing_description(event),
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    payload = get_json(session, EVENTS_API)
    events = payload.get('performances') or []
    urls = sorted({event.get('link') for event in events if event.get('link')})
    descriptions = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = [make_record(event, descriptions) for event in events]
    records = [record for record in records if record]
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class TsoCaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tso_ca',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TsoCaCrawler().run()


if __name__ == '__main__':
    main()
