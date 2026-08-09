import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://argoviaphil.ch/'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'argovia philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.8',
}

COUNTRY_CODES = {
    'austria': 'AT',
    'deutschland': 'DE',
    'france': 'FR',
    'germany': 'DE',
    'italy': 'IT',
    'liechtenstein': 'LI',
    'österreich': 'AT',
    'schweiz': 'CH',
    'switzerland': 'CH',
}


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value))
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_events(session):
    url = EVENTS_API
    params = {
        'start_date': '2000-01-01',
        'end_date': '2100-12-31',
        'per_page': 50,
        'status': 'publish',
    }
    events = []

    while url:
        response = session.get(url, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('events') or [])
        url = payload.get('next_rest_url')
        params = None

    return events


def country_code_for(venue):
    country = clean_text(venue.get('country')).lower()
    if not country:
        return 'CH'
    return COUNTRY_CODES.get(country)


def custom_field_description(event):
    fields = event.get('custom_fields') or {}
    if not isinstance(fields, dict):
        return ''

    lines = []
    for field in fields.values():
        if not isinstance(field, dict):
            continue
        label = clean_text(field.get('label'))
        value = clean_text(field.get('value'))
        if label and value:
            lines.append(f'{label}: {value}')
    return '\n'.join(lines)


def make_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    country_code = country_code_for(venue_data)

    try:
        start_at = datetime.strptime(event.get('start_date') or '', '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    # Some old entries use only the city as their venue. That is not a valid
    # venue and the API provides no defensible building name for those records.
    if not all((title, url, venue, city, country_code)) or venue.casefold() == city.casefold():
        return None

    description_parts = [clean_text(event.get('description'))]
    custom_description = custom_field_description(event)
    if custom_description:
        description_parts.append(custom_description)

    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(part for part in description_parts if part) or None,
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = fetch_events(session)
    records = []

    for event in events:
        try:
            record = make_record(event)
        except (TypeError, ValueError) as error:
            log_message(
                'Failed to parse argovia philharmonic concert',
                event='crawler_item_failed',
                level='warning',
                url=clean_text(event.get('url')),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['venue']
        ),
    )


class ArgoviaphilChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='argoviaphil_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ArgoviaphilChCrawler().run()


if __name__ == '__main__':
    main()
