import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ensemble-recherche.de/'
SOURCE = 'Ensemble Recherche'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'Accept': 'application/json',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
}

COUNTRY_CODES = {
    'belgien': 'BE', 'belgium': 'BE',
    'dänemark': 'DK', 'denmark': 'DK',
    'deutschland': 'DE', 'germany': 'DE',
    'finnland': 'FI', 'finland': 'FI',
    'frankreich': 'FR', 'france': 'FR',
    'italien': 'IT', 'italy': 'IT',
    'japan': 'JP',
    'kanada': 'CA', 'canada': 'CA',
    'luxemburg': 'LU', 'luxembourg': 'LU',
    'niederlande': 'NL', 'netherlands': 'NL',
    'norwegen': 'NO', 'norway': 'NO',
    'österreich': 'AT', 'austria': 'AT',
    'polen': 'PL', 'poland': 'PL',
    'portugal': 'PT',
    'schweden': 'SE', 'sweden': 'SE',
    'schweiz': 'CH', 'switzerland': 'CH',
    'spanien': 'ES', 'spain': 'ES',
    'tschechien': 'CZ', 'czech republic': 'CZ',
    'vereinigte staaten': 'US', 'united states': 'US', 'usa': 'US',
    'vereinigtes königreich': 'GB', 'united kingdom': 'GB',
}

# Some older venue records omit their country. These explicit touring cities
# provide stronger evidence than treating every occurrence as German.
CITY_COUNTRIES = {
    'aarhus': 'DK', 'amsterdam': 'NL', 'boston': 'US', 'brüssel': 'BE',
    'copenhagen': 'DK', 'københavn': 'DK', 'helsinki': 'FI',
    'kriens': 'CH', 'london': 'GB', 'luxembourg': 'LU', 'luxemburg': 'LU',
    'madrid': 'ES', 'milano': 'IT', 'oulu': 'FI', 'paris': 'FR',
    'shinjuku city': 'JP', 'stockholm': 'SE', 'straßburg': 'FR',
    'strasbourg': 'FR', 'tokyo': 'JP', 'warsaw': 'PL', 'warschau': 'PL',
    'wien': 'AT', 'vienna': 'AT', 'zürich': 'CH',
}


def clean_text(value):
    if not value:
        return ''
    raw = unescape(str(value))
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def clean_city(value):
    city = clean_text(value)
    return re.sub(r'^\d{4,6}\s+', '', city).strip()


def parse_start(event):
    try:
        start = datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S')
    except (KeyError, TypeError, ValueError):
        return None, None
    time_from = None if event.get('all_day') else start.strftime('%H:%M')
    return start.date().isoformat(), time_from


def country_code(venue):
    country = clean_text(venue.get('country')).casefold()
    if country in COUNTRY_CODES:
        return COUNTRY_CODES[country]

    city = clean_city(venue.get('city')).casefold().split(',')[0]
    if city in CITY_COUNTRIES:
        return CITY_COUNTRIES[city]

    address = clean_text(venue.get('address'))
    postal_code = clean_text(venue.get('zip'))
    if re.fullmatch(r'\d{5}', postal_code) or re.search(r'(?<!\d)\d{5}(?!\d)', address):
        return 'DE'
    return None


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    date, time_from = parse_start(event)
    venue_data = event.get('venue') if isinstance(event.get('venue'), dict) else {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_city(venue_data.get('city'))
    country = country_code(venue_data)
    if not all((title, date, url, venue, city, country)):
        return None
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country,
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class EnsembleRechercheDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensemble_recherche_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {'per_page': 50, 'start_date': '2000-01-01', 'page': 1}
        records = []

        while True:
            try:
                response = session.get(EVENTS_API, params=params, timeout=60)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Ensemble Recherche events',
                    event='crawler_fetch_failed', level='error', url=EVENTS_API,
                    page=params['page'], error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events')
            if not isinstance(events, list):
                raise ValueError('Events API response does not contain an events list')
            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = int(payload.get('total_pages') or 1)
            if params['page'] >= total_pages:
                break
            params['page'] += 1

        records.sort(key=lambda row: (row['date'], row['time_from'] or '', row['title']))
        return records


def main():
    EnsembleRechercheDeCrawler().run()


if __name__ == '__main__':
    main()
