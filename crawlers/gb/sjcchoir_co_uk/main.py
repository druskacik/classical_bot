import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sjcchoir.co.uk/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = "Choir of St John's College, Cambridge"

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'Denmark': 'DK',
    'Luxembourg': 'LU',
    'Netherlands': 'NL',
    'Sweden': 'SE',
    'United Kingdom': 'GB',
    'United States': 'US',
}

# A few tour records were published without structured venue data. Their detail
# text and titles explicitly identify these locations.
LOCATION_FALLBACKS = {
    'sjccg6': ('Our Lady and the English Martyrs Church', 'Cambridge', 'GB'),
    'stockholm-cathedralmusic-in-storkyrkan': ('Stockholm Cathedral', 'Stockholm', 'SE'),
    'herning-church': ('Herning Church', 'Herning', 'DK'),
    'herning-church-concert': ('Herning Church', 'Herning', 'DK'),
    'uppsala-cathedral': ('Uppsala Cathedral', 'Uppsala', 'SE'),
    'olaus-petri-orebro': ('Olaus Petri Church', 'Örebro', 'SE'),
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    for node in soup.select(
        'script, style, iframe, .tribe-events-schedule, .tribe-block__events-link, '
        '.tribe-block__event-price, .tribe-block__organizer__details, '
        '.tribe-block__venue'
    ):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalize_city(value):
    city = clean_text(value)
    # Some Dutch venue records incorrectly prefix the city with its postcode.
    return re.sub(r'^\d{4}\s*[A-Z]{2}\s+', '', city).strip()


def parse_location(event):
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = normalize_city(venue_data.get('city'))
    country = clean_text(venue_data.get('country'))

    if venue and not city and country == 'Luxembourg':
        city = 'Luxembourg'

    country_code = COUNTRY_CODES.get(country)
    if venue and city and country_code:
        return venue, city, country_code

    return LOCATION_FALLBACKS.get(event.get('slug', ''))


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    start_value = event.get('start_date')
    location = parse_location(event)
    if not title or not url or not start_value or not location:
        return None

    try:
        start = datetime.strptime(start_value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    all_day = bool(event.get('all_day'))
    end_time = None
    end_value = event.get('end_date')
    if not all_day and end_value:
        try:
            end = datetime.strptime(end_value, '%Y-%m-%d %H:%M:%S')
            if end.date() == start.date():
                end_time = end.strftime('%H:%M')
        except (TypeError, ValueError):
            pass
    venue, city, country_code = location
    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if all_day else start.strftime('%H:%M'),
        'time_to': end_time,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        params = {
            'page': page,
            'per_page': 50,
            'start_date': '2000-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
            'status': 'publish',
        }
        try:
            response = session.get(API_URL, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch SJC Choir events API',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        events = payload.get('events') or []
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class SjcChoirCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sjcchoir_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'time_to',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SjcChoirCoUkCrawler().run()


if __name__ == '__main__':
    main()
