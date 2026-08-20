import re
from datetime import datetime

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.alexandrawhittingham.com/'
SOURCE = 'Alexandra Whittingham'
API_URL = 'https://rest.bandsintown.com/artists/Alexandra%20Whittingham/events'
APP_ID = 'umg_decca_alexandrawhittingham'

COUNTRY_CODES = {
    'Austria': 'AT',
    'Canada': 'CA',
    'France': 'FR',
    'Germany': 'DE',
    'United Kingdom': 'GB',
    'United States': 'US',
}

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', value).strip()


def is_location_label(value, city, country):
    value = clean_text(value).casefold()
    city = clean_text(city).casefold()
    country = clean_text(country).casefold()
    if not value or not city:
        return True
    if value == city or value.startswith(city + ',') or value.startswith(city + ' -'):
        return True
    if re.search(r'(?:,\s*[A-Z]{2}|\s+-?\s*(?:UK|DE))$', clean_text(value), re.IGNORECASE):
        return True
    return value in {f'{city} {country}', f'{city}, {country}'}


def venue_name(event):
    venue = event.get('venue') or {}
    name = clean_text(venue.get('name'))
    city = clean_text(venue.get('city'))
    country = clean_text(venue.get('country'))
    if not is_location_label(name, city, country):
        return name

    # Bandsintown sometimes puts the real venue in street_address while using
    # the city as its venue name. Accept only clearly named places, not an
    # address, postcode, or another copy of the city.
    description = clean_text(event.get('description'))
    match = re.search(r'\b(?:at|in the)\s+([^.;]+)', description, flags=re.IGNORECASE)
    if match:
        candidate = clean_text(match.group(1)).strip(' ,')
        if candidate and not is_location_label(candidate, city, country):
            return candidate

    street = clean_text(venue.get('street_address'))
    address_suffix = r'\b(?:rd|road|st|street|ave|avenue|dr|drive|blvd|boulevard|lane|ln)\.?$'
    if (
        street
        and not re.search(r'\d', street)
        and not re.search(address_suffix, street, re.IGNORECASE)
        and not is_location_label(street, city, country)
    ):
        return street
    return ''


def event_title(event):
    venue = event.get('venue') or {}
    title = clean_text(event.get('title'))
    if title and not is_location_label(title, venue.get('city'), venue.get('country')):
        return title
    return SOURCE


def parse_event(event):
    venue = event.get('venue') or {}
    city = clean_text(venue.get('city'))
    country_code = COUNTRY_CODES.get(clean_text(venue.get('country')))
    place = venue_name(event)
    url = clean_text(event.get('url'))
    try:
        start = datetime.fromisoformat(clean_text(event.get('datetime')).replace('Z', '+00:00'))
    except ValueError:
        return None

    if not city or not country_code or not place or not url:
        return None

    description = clean_text(event.get('description')) or None
    return {
        'title': event_title(event),
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': place,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_events():
    response = requests.get(
        API_URL,
        params={'app_id': APP_ID, 'date': 'all'},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError('Bandsintown events response was not a list')

    records = []
    skipped = 0
    for event in payload:
        record = parse_event(event) if isinstance(event, dict) else None
        if record:
            records.append(record)
        else:
            skipped += 1
    if skipped:
        log_message(
            'Skipped events without a defensible venue or required location data',
            event='crawler_items_skipped',
            level='warning',
            skipped_count=skipped,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class AlexandraWhittinghamComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='alexandrawhittingham_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_events()


def main():
    AlexandraWhittinghamComCrawler().run()


if __name__ == '__main__':
    main()
