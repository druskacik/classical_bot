import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jonathanbiss.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Jonathan Biss'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'Australia': 'AU',
    'Austria': 'AT',
    'Belgium': 'BE',
    'Canada': 'CA',
    'Czechia': 'CZ',
    'Denmark': 'DK',
    'Estonia': 'EE',
    'Finland': 'FI',
    'France': 'FR',
    'Germany': 'DE',
    'Hungary': 'HU',
    'Ireland': 'IE',
    'Israel': 'IL',
    'Italy': 'IT',
    'Japan': 'JP',
    'Netherlands': 'NL',
    'Philippines': 'PH',
    'Poland': 'PL',
    'Singapore': 'SG',
    'Spain': 'ES',
    'Sweden': 'SE',
    'Switzerland': 'CH',
    'Taiwan': 'TW',
    'United Kingdom': 'GB',
    'United States': 'US',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_location(location):
    address_line = clean_text(location.get('addressLine2'))
    city = address_line.split(',', 1)[0].strip()
    if not city or re.search(r'\b(?:street|st\.?|road|rd\.?|avenue|ave\.?)$', city, re.I):
        return ''
    return city


def venue_from_location(location):
    venue = clean_text(location.get('addressTitle'))
    if venue:
        return venue

    # Some records put a named building, rather than a street address, in
    # addressLine1. Do not turn an ordinary address into a venue.
    address_line = clean_text(location.get('addressLine1'))
    if address_line and not re.search(
        r'\d|\b(?:street|st\.?|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?|place|pl\.?)\b',
        address_line,
        re.I,
    ):
        return address_line
    return ''


def description_from_event(event):
    for field in ('body', 'excerpt'):
        text = clean_text(event.get(field))
        if text:
            return text
    return None


def parse_event(event, timezone):
    title = clean_text(event.get('title'))
    full_url = clean_text(event.get('fullUrl'))
    location = event.get('location') or {}
    venue = venue_from_location(location)
    city = city_from_location(location)
    country_name = clean_text(location.get('addressCountry'))
    country_code = COUNTRY_CODES.get(country_name, '')

    try:
        start = datetime.fromtimestamp(event['startDate'] / 1000, timezone)
        event_date = start.date().isoformat()
        time_from = start.strftime('%H:%M')
    except (KeyError, TypeError, ValueError, OSError):
        event_date = ''
        time_from = None

    url = urljoin(SOURCE_URL, full_url) if full_url else ''
    if not all((title, event_date, url, venue, city, country_code)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_from_event(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_events(session):
    params = {'format': 'json'}
    events = {}
    seen_offsets = set()
    timezone = ZoneInfo('America/New_York')

    while True:
        response = session.get(CALENDAR_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()

        timezone_name = clean_text((payload.get('website') or {}).get('timeZone'))
        if timezone_name:
            try:
                timezone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                pass

        for event in (payload.get('upcoming') or []) + (payload.get('past') or []):
            event_id = clean_text(event.get('id'))
            if event_id:
                events[event_id] = event

        pagination = payload.get('pagination') or {}
        if not pagination.get('nextPage'):
            break
        offset = pagination.get('nextPageOffset')
        if offset is None or offset in seen_offsets:
            raise ValueError('Squarespace calendar returned an invalid pagination offset')
        seen_offsets.add(offset)
        params = {'format': 'json', 'offset': offset}

    return list(events.values()), timezone


class JonathanBissComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jonathanbiss_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events, timezone = fetch_events(session)
        records = []

        for event in events:
            record = parse_event(event, timezone)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Jonathan Biss event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=urljoin(SOURCE_URL, clean_text(event.get('fullUrl'))),
                    error_type='IncompleteEventData',
                    error_message=(
                        'Required title, date, URL, venue, city, or country is missing'
                    ),
                )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    JonathanBissComCrawler().run()


if __name__ == '__main__':
    main()
