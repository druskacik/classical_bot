import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filharmonija.lt/'
SOURCE = 'Lietuvos nacionalinė filharmonija'
LISTING_PATHS = (
    '/repertuaras/koncertai-vilniuje/53',
    '/repertuaras/koncertai-kituose-miestuose/54',
)
PAGE_SIZE = 500

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'lt-LT,lt;q=0.9,en;q=0.7',
}

COUNTRY_SUFFIXES = {
    'Azerbaidžanas': 'AZ',
    'Bulgarija': 'BG',
    'Danija': 'DK',
    'Estija': 'EE',
    'Ispanija': 'ES',
    'Italija': 'IT',
    'Japonija': 'JP',
    'JAV': 'US',
    'Kanada': 'CA',
    'Latvija': 'LV',
    'Lenkija': 'PL',
    'Malta': 'MT',
    'Moldovos Respublika': 'MD',
    'Nyderlandai': 'NL',
    'Pietų Korėja': 'KR',
    'Portugalija': 'PT',
    'Prancūzija': 'FR',
    'Sakartvelas': 'GE',
    'Suomija': 'FI',
    'Švedija': 'SE',
    'Šveicarija': 'CH',
    'Taivanas': 'TW',
    'Turkija': 'TR',
    'Vokietija': 'DE',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_feed_page(session, listing_path, start):
    params = {
        'date_from': '2000-01-01',
        'date_to': '',
        'city_id': '',
        'location_id': '',
        'genre_id': '',
        'artist_id': '',
        'composer_id': '',
        'organizer_id': '',
        'search_text': '',
    }
    response = session.get(
        urljoin(SOURCE_URL, f'{listing_path}/data'),
        params=params,
        headers={'Range': f'{start}-{start + PAGE_SIZE - 1}', 'Range-Unit': 'items'},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    events = []
    for month in (payload.get('list') or {}).values():
        events.extend(month.get('list') or [])

    content_range = response.headers.get('Content-Range', '')
    match = re.search(r'/([0-9]+)$', content_range)
    total = int(match.group(1)) if match else len(events)
    return events, total


def get_events(session, listing_path):
    events = []
    start = 0
    while True:
        page, total = get_feed_page(session, listing_path, start)
        events.extend(page)
        start += len(page)
        if not page or start >= total:
            return events


def resolve_city(raw_city):
    city = clean_text(raw_city)
    if not city:
        return None, None
    for suffix, country_code in COUNTRY_SUFFIXES.items():
        marker = f', {suffix}'
        if city.endswith(marker):
            return city[:-len(marker)].strip(), country_code
    return city, 'LT'


def event_datetime(timestamp):
    try:
        value = int(timestamp)
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, tz=ZoneInfo('Europe/Vilnius'))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def event_description(event):
    parts = []
    for heading, key in (
        ('Aprašymas', 'full_text'),
        ('Atlikėjai', 'artist_text'),
        ('Programa', 'program_text'),
    ):
        text = clean_text(event.get(key))
        if text and text not in parts:
            parts.append(f'{heading}\n{text}')
    return '\n\n'.join(parts) or None


def make_record(event):
    title = clean_text(event.get('title'))
    venue = clean_text(event.get('location_title'))
    city, country_code = resolve_city(event.get('city_title'))
    starts_at = event_datetime(event.get('date'))
    path = event.get('view_url')
    url = urljoin(SOURCE_URL, path) if path else ''
    if not title or not venue or not city or not country_code or not starts_at or not url:
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': event_description(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for listing_path in LISTING_PATHS:
        try:
            events = get_events(session, listing_path)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape concert feed',
                event='crawler_feed_failed',
                level='warning',
                url=urljoin(SOURCE_URL, listing_path),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        records.extend(record for event in events if (record := make_record(event)))

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FilharmonijaLtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonija_lt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LT',
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
    FilharmonijaLtCrawler().run()


if __name__ == '__main__':
    main()
