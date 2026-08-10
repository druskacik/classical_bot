import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ruhrtriennale.de/de'
SOURCE = 'Ruhrtriennale'
API_URL = f'{SOURCE_URL}/api/'
PRODUCTIONS_URL = f'{API_URL}productions/'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=8,
        pool_maxsize=8,
        max_retries=Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def paginated_json(session, url, params=None):
    results = []
    next_url = url
    next_params = params
    while next_url:
        response = session.get(next_url, params=next_params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        results.extend(payload.get('results') or [])
        next_url = payload.get('next')
        next_params = None
    return results


def cast_text(casts):
    parts = []
    for item in casts or []:
        if not isinstance(item, dict):
            continue
        values = []
        for key in ('role', 'function', 'description', 'name', 'text', 'title'):
            value = item.get(key)
            if isinstance(value, dict):
                value = value.get('description') or value.get('name')
            value = clean_text(value)
            if value and value not in values:
                values.append(value)
        if values:
            parts.append(': '.join(values))
    return '\n'.join(parts)


def production_description(production):
    parts = []
    for key in (
        'production_type_description', 'subtitle', 'text_title', 'intro',
        'introduction', 'short_text', 'about_production_text',
        'language_information', 'text_cooperation',
    ):
        value = clean_text(production.get(key))
        if value and value not in parts:
            parts.append(value)
    casts = cast_text(production.get('casts'))
    if casts and casts not in parts:
        parts.append(casts)
    return '\n\n'.join(parts) or None


def parse_booking(production, booking):
    title = clean_text(production.get('name')) or clean_text(booking.get('name'))
    start_raw = booking.get('date_start')
    try:
        start = datetime.fromisoformat(start_raw)
    except (TypeError, ValueError):
        return None

    venue_data = booking.get('venue') or production.get('venue') or {}
    city = clean_text(booking.get('city')) or clean_text(venue_data.get('city'))
    venue = clean_text(booking.get('room_display_text'))
    # Some multi-location bookings put only the city in room_display_text.
    # A city is not a venue; prefer the API's actual venue description there.
    if not venue or venue.casefold() == city.casefold():
        venue = (
            clean_text(booking.get('venue_description'))
            or clean_text(venue_data.get('description'))
        )
    relative_url = production.get('get_absolute_url')
    if not relative_url:
        slug = (production.get('slug') or {}).get('de')
        if slug and production.get('id'):
            relative_url = f'/de/programm/{slug}/{production["id"]}'
    url = urljoin(SOURCE_URL, relative_url or '')
    if not all((title, venue, city, relative_url)):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': production_description(production),
    }


def scrape_production(session, summary):
    production_id = summary['id']
    response = session.get(f'{PRODUCTIONS_URL}{production_id}/', timeout=45)
    response.raise_for_status()
    production = response.json()
    bookings = paginated_json(
        session, f'{API_URL}bookings/', params={'production': production_id}
    )
    return [
        record for booking in bookings
        if (record := parse_booking(production, booking))
    ]


class RuhrtriennaleDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ruhrtriennale_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        productions = paginated_json(session, PRODUCTIONS_URL)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(scrape_production, session, production): production
                for production in productions
            }
            for future in as_completed(futures):
                production = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Ruhrtriennale production',
                        event='crawler_item_failed',
                        level='warning',
                        url=f'{PRODUCTIONS_URL}{production.get("id")}/',
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['city'], item['title']
        ))


def main():
    RuhrtriennaleDeCrawler().run()


if __name__ == '__main__':
    main()
