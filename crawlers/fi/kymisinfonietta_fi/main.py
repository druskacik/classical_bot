import html
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://kymisinfonietta.fi/'
SOURCE = 'Kymi Sinfonietta'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
ARCHIVE_START = '2000-01-01'
ARCHIVE_END = '2100-12-31'
PAGE_SIZE = 50
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}
COUNTRY_CODES = {
    'finland': 'FI',
    'finska': 'FI',
    'suomi': 'FI',
    'estonia': 'EE',
    'eesti': 'EE',
    'sweden': 'SE',
    'sverige': 'SE',
    'ruotsi': 'SE',
}
CITY_HINTS = {
    'tallinna': ('Tallinn', 'EE'),
    'kotka': ('Kotka', 'FI'),
    'kuusankoski': ('Kouvola', 'FI'),
    'kouvola': ('Kouvola', 'FI'),
    'ratamo': ('Kouvola', 'FI'),
    'satama areena': ('Kotka', 'FI'),
    'kymenlaakson keskussairaala': ('Kotka', 'FI'),
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    text = soup.get_text('\n', strip=True)
    lines = [' '.join(line.replace('\xa0', ' ').split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def parse_start(value, all_day=False):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), None if all_day else parsed.strftime('%H:%M')


def country_code(value):
    if not value:
        return None
    normalized = ' '.join(str(value).split()).casefold()
    return COUNTRY_CODES.get(normalized)


def resolve_location(venue_data):
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    venue_country = venue_data.get('country')

    # The archive contains a few incomplete venue objects. Their venue names
    # identify the municipality unambiguously, including the Tallinn tour.
    searchable = venue.casefold()
    for token, location in CITY_HINTS.items():
        if token in searchable:
            hinted_city, hinted_country = location
            if not city or hinted_country != 'FI':
                return venue, hinted_city, hinted_country

    event_country_code = country_code(venue_country) if venue_country else 'FI'
    return venue, city, event_country_code


def parse_event(event):
    venue_data = event.get('venue') or {}
    title = clean_text(html.unescape(event.get('title') or ''))
    event_date, time_from = parse_start(event.get('start_date'), event.get('all_day', False))
    url = (event.get('url') or '').strip()
    venue, city, event_country_code = resolve_location(venue_data)
    description = clean_text(event.get('description')) or None

    if not all((title, event_date, url, venue, city, event_country_code)):
        log_message(
            'Skipping Kymi Sinfonietta event with incomplete required fields',
            event='crawler_item_skipped',
            level='warning',
            url=url or API_URL,
            event_id=event.get('id'),
        )
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': event_country_code,
        'description': description,
    }


def fetch_page(session, page):
    response = session.get(
        API_URL,
        params={
            'per_page': PAGE_SIZE,
            'page': page,
            'start_date': ARCHIVE_START,
            'end_date': ARCHIVE_END,
            'status': 'publish',
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload.get('events'), list):
        raise ValueError('Kymi Sinfonietta API response has no events list')
    return payload


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    records = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        payload = fetch_page(session, page)
        try:
            total_pages = int(payload.get('total_pages', 1))
        except (TypeError, ValueError) as error:
            raise ValueError('Kymi Sinfonietta API returned invalid pagination') from error
        for event in payload['events']:
            record = parse_event(event)
            if record:
                records.append(record)
        page += 1

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class KymiSinfoniettaFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kymisinfonietta_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    KymiSinfoniettaFiCrawler().run()


if __name__ == '__main__':
    main()
