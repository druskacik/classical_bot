import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ensemble360.co.uk/'
SOURCE = 'Ensemble 360'
API_URL = 'https://ensemble360.co.uk/wp-json/tribe/events/v1/events'
ENSEMBLE_360_CATEGORY_ID = 4

NON_CITY_LOCALITIES = {
    'Buckinghamshire',
    'Cheshire',
    'East Riding of Yorkshire',
    'Essex',
    'Hampshire',
    'Hertfordshire',
    'Kent',
    'Norfolk',
    'Nottinghamshire',
    'Wiltshire',
}

# A few source venue rows put a county in the city field. These venue names
# provide stronger, unambiguous locality evidence than that field.
VENUE_CITY_HINTS = {
    'Chelmsford Theatre': 'Chelmsford',
    'Dagenham Park Church of England School': 'Dagenham',
    'Junction Goole': 'Goole',
    'LSE Shaw Library': 'London',
    'Portsmouth Guildhall': 'Portsmouth',
}

COUNTRY_CODES = {
    'England': 'GB',
    'Isle of Man': 'IM',
    'Northern Ireland': 'GB',
    'Scotland': 'GB',
    'United Kingdom': 'GB',
    'Wales': 'GB',
    'Spain': 'ES',
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        value = str(value)
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def infer_city(venue, title):
    city = clean_text(venue.get('city'))
    if city and city not in NON_CITY_LOCALITIES:
        return city

    venue_name = clean_text(venue.get('venue'))
    address = clean_text(venue.get('address'))
    # Some imported venue rows omit the city field but repeat the locality
    # after a comma in the venue name or street address.
    for value in (venue_name, address):
        if ',' not in value:
            continue
        candidate = value.rsplit(',', 1)[-1].strip()
        candidate = re.sub(r'^\d{4,6}\s+', '', candidate)
        if candidate and not re.search(r'\d', candidate):
            return candidate
    for venue_hint, locality in VENUE_CITY_HINTS.items():
        if venue_hint.casefold() in venue_name.casefold():
            return locality
    if ':' in title:
        candidate = title.rsplit(':', 1)[-1].strip()
        if candidate and candidate not in NON_CITY_LOCALITIES:
            return candidate
    return None


def infer_country_code(venue):
    country = clean_text(venue.get('country'))
    if not country:
        return 'GB'
    return COUNTRY_CODES.get(country)


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    parsed_start = parse_start(event.get('start_date'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = infer_city(venue_data, title)
    country_code = infer_country_code(venue_data)

    if not title or not url or not parsed_start or not venue or not city or not country_code:
        return None

    event_date, time_from = parsed_start
    if event.get('all_day'):
        time_from = None
    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class Ensemble360CoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensemble360_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        skipped_count = 0
        page = 1

        while True:
            params = {
                'per_page': 50,
                'page': page,
                'start_date': '1900-01-01',
                'end_date': '2100-12-31',
                'categories': ENSEMBLE_360_CATEGORY_ID,
            }
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Ensemble 360 events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events')
            if not isinstance(events, list):
                raise ValueError('Ensemble 360 API response has no events list')

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    skipped_count += 1

            total_pages = payload.get('total_pages', 1)
            if not isinstance(total_pages, int) or total_pages < 1:
                raise ValueError('Ensemble 360 API returned invalid pagination')
            if page >= total_pages:
                break
            page += 1

        if skipped_count:
            log_message(
                'Skipped Ensemble 360 events with incomplete required fields',
                event='crawler_records_skipped',
                level='warning',
                record_count=skipped_count,
            )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    Ensemble360CoUkCrawler().run()


if __name__ == '__main__':
    main()
