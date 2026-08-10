import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://copenhagenphil.dk/'
SOURCE = 'Copenhagen Phil'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'da-DK,da;q=0.9,en;q=0.7',
}

COUNTRY_CODES = {
    'danmark': 'DK',
    'denmark': 'DK',
    'austria': 'AT',
    'østrig': 'AT',
    'germany': 'DE',
    'tyskland': 'DE',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    if '<' in raw:
        soup = BeautifulSoup(raw, 'html.parser')
        for unwanted in soup.select('script, style, noscript'):
            unwanted.decompose()
        text = soup.get_text('\n', strip=True)
    else:
        text = raw
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_city(value, venue):
    city = clean_text(value)
    city = re.sub(r'^\d{4,5}\s*', '', city).strip()
    city = re.sub(r'^(København|Frederiksberg)\s+[CKNV]$', r'\1', city, flags=re.I)
    if city == 'Nykøbing F':
        city = 'Nykøbing Falster'
    if city:
        return city

    # One archived venue has only a postal code in the city field, while its
    # venue name explicitly identifies the town.
    if re.search(r'\bRønne\b', venue, re.I):
        return 'Rønne'
    return ''


def parse_country(value):
    country = clean_text(value).lower()
    return COUNTRY_CODES.get(country, 'DK' if not country else '')


def make_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = event.get('start_date') or ''
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}):(\d{2}):\d{2})?', start)
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = parse_city(venue_data.get('city'), venue)
    country_code = parse_country(venue_data.get('country'))

    if not title or not url or not match or not venue or not city or not country_code:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None
    if not event.get('all_day') and match.group(2):
        time_from = f'{match.group(2)}:{match.group(3)}'

    description = clean_text(event.get('description')) or clean_text(event.get('excerpt')) or None
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


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    params = {
        'per_page': 50,
        'start_date': '2000-01-01',
        'end_date': '2100-12-31',
        'status': 'publish',
        'page': 1,
    }
    records = []

    while True:
        response = session.get(EVENTS_API, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []
        for event in events:
            record = make_record(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped concert with incomplete event data',
                    event='crawler_item_skipped',
                    level='warning',
                    url=clean_text(event.get('url')) or EVENTS_API,
                )

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class CopenhagenPhilDkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='copenhagenphil_dk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DK',
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
    CopenhagenPhilDkCrawler().run()


if __name__ == '__main__':
    main()
