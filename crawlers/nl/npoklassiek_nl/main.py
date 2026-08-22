import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.npoklassiek.nl/evenementen'
SOURCE = 'NPO Klassiek'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}
MONTHS = {
    'januari': 1,
    'februari': 2,
    'maart': 3,
    'april': 4,
    'mei': 5,
    'juni': 6,
    'juli': 7,
    'augustus': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'december': 12,
}
VENUE_CITIES = {
    'het concertgebouw': 'Amsterdam',
    'concertgebouw': 'Amsterdam',
    'tivolivredenburg': 'Utrecht',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    node = soup.select_one('script#__NEXT_DATA__')
    if not node or not node.string:
        raise ValueError('Page does not contain Next.js data')
    return json.loads(node.string).get('props', {}).get('pageProps', {})


def listing_events(session):
    events = []
    page = 1
    max_page = 1
    while page <= max_page:
        payload = get_page(session, SOURCE_URL, params={'page': page})
        events.extend(payload.get('events') or [])
        max_page = int((payload.get('pagination') or {}).get('maxPage') or page)
        page += 1
    return events


def parse_dutch_datetime(value):
    match = re.search(
        r'(\d{1,2})\s+([a-z]+)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?',
        clean_text(value).lower(),
    )
    if not match or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    event_time = None
    if match.group(4):
        hour, minute = int(match.group(4)), int(match.group(5))
        if hour < 24 and minute < 60:
            event_time = f'{hour:02d}:{minute:02d}'
    return event_date, event_time


def description_from(concert):
    parts = []
    introduction = clean_text(concert.get('introduction'))
    if introduction:
        parts.append(introduction)
    for block in concert.get('body') or []:
        value = block.get('value') or {}
        text = clean_text(value.get('text')) if isinstance(value, dict) else ''
        if not text or text.lower() in {'bestel kaarten', 'koop kaarten'}:
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_location(value):
    parts = [part.strip() for part in clean_text(value).split(',') if part.strip()]
    if len(parts) < 2:
        return '', ''
    known_city = VENUE_CITIES.get(parts[0].lower())
    if known_city:
        return known_city, ', '.join(parts)
    return parts[0], ', '.join(parts[1:])


def make_record(listing, payload):
    concert = payload.get('concert') or {}
    title = clean_text(payload.get('pageHeaderTitle') or listing.get('title'))
    path = listing.get('url') or (payload.get('seo') or {}).get('canonicalUrl')
    url = urljoin(SOURCE_URL, path) if path else ''
    event_date, time_from = parse_dutch_datetime(
        concert.get('startAt') or listing.get('date')
    )

    city, venue = parse_location(concert.get('location'))
    if not title or not event_date or not url or not city or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'NL',
        'description': description_from(concert),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_record(listing):
    session = requests.Session()
    session.headers.update(HEADERS)
    url = urljoin(SOURCE_URL, listing.get('url') or '')
    return make_record(listing, get_page(session, url))


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_record, event): event
            for event in events
            if event.get('url')
        }
        for future in as_completed(futures):
            event = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=urljoin(SOURCE_URL, event.get('url') or ''),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class NpoKlassiekNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='npoklassiek_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
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
        return get_concerts()


def main():
    NpoKlassiekNlCrawler().run()


if __name__ == '__main__':
    main()
