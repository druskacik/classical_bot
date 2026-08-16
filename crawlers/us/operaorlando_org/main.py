import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operaorlando.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Opera Orlando'
WIDGET_ID = 'fb6f385d-1cf7-4171-b90f-3158d28eecf1'
API_URL = 'https://core.service.elfsight.com/p/boot/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Referer': CALENDAR_URL,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return ''


def parse_time(value):
    if re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(value or '')):
        return str(value)
    return None


def city_from_location(location):
    address = clean_text(location.get('address'))
    # Elfsight stores US addresses as free text. The locality immediately
    # before "FL" is stable for the public venues in this calendar.
    match = re.search(r'(?:,|\s)\s*([A-Za-z][A-Za-z .]+?),?\s+FL(?:orida)?\s+\d{5}\b', address, re.I)
    if match:
        city = clean_text(match.group(1)).strip(', ')
        # With no comma before the locality, retain only the final words known
        # to form the Central Florida city name.
        for known_city in ('Winter Park', 'The Villages', 'Orlando', 'Montverde', 'Windermere'):
            if city.lower().endswith(known_city.lower()):
                return known_city

    lower_address = address.lower()
    for known_city in ('Winter Park', 'The Villages', 'Orlando', 'Montverde', 'Windermere'):
        if known_city.lower() in lower_address:
            return known_city

    # This private-home listing omits its city, but its street is in Orlando
    # and the calendar explicitly presents it as an Opera Orlando event.
    if 'ivanhoe blvd' in lower_address:
        return 'Orlando'
    return ''


def event_url(event):
    actions = event.get('actions') or []
    for primary in (True, False):
        for action in actions:
            if action.get('primary') is primary:
                value = (action.get('link') or {}).get('value')
                if value:
                    if re.match(r'^[A-Za-z0-9.-]+\.[A-Za-z]{2,}/', value):
                        value = f'https://{value}'
                    return urljoin(SOURCE_URL, value)
    event_id = clean_text(event.get('id'))
    return f'{CALENDAR_URL}#event={event_id}' if event_id else CALENDAR_URL


def widget_settings(session=None):
    session = session or requests.Session()
    response = session.get(
        API_URL,
        params={'page': CALENDAR_URL, 'w': WIDGET_ID},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    return payload['data']['widgets'][WIDGET_ID]['data']['settings']


def scrape_concerts(session=None):
    try:
        settings = widget_settings(session)
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        log_message(
            'Unable to load Opera Orlando calendar API',
            event='crawler_request_failed',
            level='error',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    locations = {
        str(item.get('id')): item
        for item in settings.get('locations', [])
        if item.get('id')
    }
    records = []
    skipped = 0

    for event in settings.get('events', []):
        title = clean_text(event.get('name'))
        event_date = parse_date((event.get('start') or {}).get('date'))
        location_ids = event.get('location') or []
        location = locations.get(str(location_ids[0])) if location_ids else None
        venue = clean_text((location or {}).get('name'))
        city = city_from_location(location or {})

        if not title or not event_date or not venue or not city:
            skipped += 1
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': event_url(event),
            'time_from': parse_time((event.get('start') or {}).get('time')),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': clean_text(event.get('description')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if skipped:
        log_message(
            'Skipped incomplete Opera Orlando calendar events',
            event='crawler_records_skipped',
            level='warning',
            url=CALENDAR_URL,
            record_count=skipped,
        )
    if not records:
        log_message(
            'No valid Opera Orlando calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class OperaOrlandoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaorlando_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    OperaOrlandoOrgCrawler().run()


if __name__ == '__main__':
    main()
