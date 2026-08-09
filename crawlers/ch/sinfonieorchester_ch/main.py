import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sinfonieorchester.ch/de/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Luzerner Sinfonieorchester'

HEADERS = {
    'Accept': 'application/json',
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

COUNTRY_CODES = {
    'schweiz': 'CH',
    'switzerland': 'CH',
    'deutschland': 'DE',
    'frankreich': 'FR',
    'italien': 'IT',
    'italy': 'IT',
    'österreich': 'AT',
    'netherlands': 'NL',
    'tschechien': 'CZ',
    'slowenien': 'SI',
    "korea, democratic people's republic of": 'KR',
}

# The Events Calendar API has incomplete city fields on a number of old venue
# records. These are stable venue-specific defaults, not general tour defaults.
VENUE_CITIES = {
    'Neubad (Pool) Luzern': 'Luzern',
    'Orchesterhaus': 'Kriens',
    'Orchesterhaus, Orchestersaal': 'Kriens',
    'KKL Luzern, Konzertsaal': 'Luzern',
    'KKL Luzern, Luzernersaal': 'Luzern',
    'KKL Luzern, Terrassensaal': 'Luzern',
    'KKL, Deuxième': 'Luzern',
    'Luzerner Theater': 'Luzern',
    'Luzerner Theater, Box': 'Luzern',
    'Theaterplatz, Luzerner Theater': 'Luzern',
    'Tonhalle, Zürich': 'Zürich',
    'Konserthuset, Stockholm': 'Stockholm',
    'Grosses Theater von Pompeii, Italien': 'Pompeii',
    'Tonhalle Düsseldorf, Mendelssohn-Saal': 'Düsseldorf',
    'Elbphilharmonie Hamburg, Grosser Saal': 'Hamburg',
    'HCC Hannover Congress Centrum, Kuppelsaal': 'Hannover',
    'Kölner Philharmonie': 'Köln',
    'Dom Santa Maria Assunta Pisa': 'Pisa',
    'Rudolfinum, Dvořák Hall': 'Prag',
    'Bruckner Haus': 'Linz',
    'Hotel Schweizerhof, Luzern': 'Luzern',
    'Hotel Beau Séjour': 'Luzern',
    'Kirche Saanen': 'Saanen',
    'Kunstmuseum Luzern': 'Luzern',
    'Teatro Dal Verme': 'Mailand',
    'Jazzkantine': 'Luzern',
    'Südpol': 'Kriens',
    'SommerCafé beim Richard Wagner Museum': 'Luzern',
    'The UniverSE Concert Hall': 'Yongin',
    'SEOUL ARTS CENTER': 'Seoul',
}

CITY_COUNTRIES = {
    'Aix-en-Provence': 'FR', 'Amsterdam': 'NL', 'Andermatt': 'CH',
    'Besançon': 'FR', 'Daejeon': 'KR', 'Düsseldorf': 'DE',
    'Engelberg': 'CH', 'Feldkirch': 'AT', 'Fermo': 'IT', 'Ferrara': 'IT',
    'Genf': 'CH', 'Grafenegg': 'AT', 'Hamburg': 'DE', 'Hannover': 'DE',
    'Köln': 'DE', 'Kriens': 'CH', 'Künzelsau': 'DE', 'Linz': 'AT',
    'Ljubljana': 'SI', 'Lubljana': 'SI', 'Lugano': 'CH', 'Luzern': 'CH',
    'Mailand': 'IT', 'München': 'DE', 'Paris': 'FR', 'Pisa': 'IT',
    'Pompeii': 'IT', 'Pordenone': 'IT', 'Prag': 'CZ', 'Rolle': 'CH',
    'Saanen': 'CH', 'Seoul': 'KR', 'Sion': 'CH', 'Stockholm': 'SE',
    'Stresa': 'IT', 'Weggis': 'CH', 'Wien': 'AT', 'Yongin': 'KR',
    'Zürich': 'CH',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '2000-01-01',
                'end_date': '2100-12-31',
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('events') or [])
        if page >= int(payload.get('total_pages') or 1):
            return events
        page += 1


def parse_city(venue):
    raw_city = clean_text(venue.get('city'))
    if raw_city and not re.fullmatch(r'\d{4,6}', raw_city):
        return raw_city.strip(' ,')
    return VENUE_CITIES.get(clean_text(venue.get('venue')), '')


def parse_country_code(venue, city):
    country = clean_text(venue.get('country')).lower()
    if country in COUNTRY_CODES:
        return COUNTRY_CODES[country]
    return CITY_COUNTRIES.get(city)


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = parse_city(venue_data)
    country_code = parse_country_code(venue_data, city)
    start = event.get('start_date') or ''
    if not all((title, url, venue, city, country_code, start)):
        return None

    try:
        start_at = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None

    time_from = None if event.get('all_day') else start_at.strftime('%H:%M')
    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
    }


class SinfonieorchesterChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sinfonieorchester_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = fetch_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Luzerner Sinfonieorchester events',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped Luzerner Sinfonieorchester event with incomplete data',
                    event='crawler_item_skipped',
                    level='warning',
                    url=event.get('url'),
                )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SinfonieorchesterChCrawler().run()


if __name__ == '__main__':
    main()
