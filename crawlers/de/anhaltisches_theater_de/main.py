import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://anhaltisches-theater.de/'
SOURCE = 'Anhaltisches Theater Dessau'
EVENTS_URL = f'{SOURCE_URL}api/termine/2000-01-01'
LOCAL_TIMEZONE = ZoneInfo('Europe/Berlin')

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
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def city_for_venue(venue):
    normalized = clean_text(venue).casefold()
    if not normalized or normalized == 'schule':
        return None
    if 'berlin' in normalized:
        return 'Berlin'
    if 'bitterfeld' in normalized:
        return 'Bitterfeld-Wolfen'
    if 'zerbst' in normalized:
        return 'Zerbst/Anhalt'
    if 'wörlitz' in normalized:
        return 'Oranienbaum-Wörlitz'
    if 'oranienbaum' in normalized:
        return 'Oranienbaum-Wörlitz'

    # The remaining named venues in the theatre's calendar are its own
    # stages or well-known performance sites within Dessau-Roßlau.
    return 'Dessau-Roßlau'


def get_json(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.json()


def detail_description(session, slug):
    payload = get_json(session, f'{SOURCE_URL}api/seite/{slug}')
    if not isinstance(payload, list) or not payload:
        return None
    detail = payload[0]
    parts = [
        clean_text(detail.get('untertitel')),
        clean_text(detail.get('text')),
    ]
    return '\n\n'.join(part for part in parts if part) or None


def listing_record(item):
    if item.get('genre') != 'Konzert' or item.get('entfaellt'):
        return None

    title = clean_text(item.get('titel') or item.get('va-titel'))
    venue = clean_text(item.get('ort'))
    city = city_for_venue(venue)
    slug = clean_text(item.get('stueck_kuerzel'))
    start_value = item.get('beginn')
    if not all((title, venue, city, slug, start_value)):
        return None

    try:
        start = datetime.fromisoformat(start_value.replace('Z', '+00:00'))
        start = start.astimezone(LOCAL_TIMEZONE)
    except (TypeError, ValueError):
        return None

    description_parts = [
        clean_text(item.get('untertitel')),
        clean_text(item.get('stueck_l1')),
        clean_text(item.get('stueck_l2')),
        clean_text(item.get('va_l1')),
        clean_text(item.get('va_l2')),
    ]
    description = '\n'.join(dict.fromkeys(x for x in description_parts if x))
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': f'{SOURCE_URL}{slug}',
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
        '_slug': slug,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    payload = get_json(session, EVENTS_URL)
    records = [record for item in payload if (record := listing_record(item))]

    slugs = {record['_slug'] for record in records}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, slug): slug for slug in slugs
        }
        for future in as_completed(futures):
            slug = futures[future]
            try:
                descriptions[slug] = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=f'{SOURCE_URL}{slug}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record.pop('_slug'))
        if detail:
            record['description'] = detail

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class AnhaltischesTheaterDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='anhaltisches_theater_de',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    AnhaltischesTheaterDeCrawler().run()


if __name__ == '__main__':
    main()
