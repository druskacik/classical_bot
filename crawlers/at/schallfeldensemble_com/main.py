import html
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.schallfeldensemble.com/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Schallfeld Ensemble'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.8,de;q=0.6',
    # Brotli negotiation makes this host reject some non-browser clients.
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}

COUNTRY_CODES = {
    'Austria': 'AT',
    'Österreich': 'AT',
    'Azerbaijan': 'AZ',
    'Belgium': 'BE',
    'Croatia (Local Name: Hrvatska)': 'HR',
    'Czech Republic': 'CZ',
    'Denmark': 'DK',
    'Finland': 'FI',
    'Finnland': 'FI',
    'Germany': 'DE',
    'Hungary': 'HU',
    'Italy': 'IT',
    'Poland': 'PL',
    'Portugal': 'PT',
    'Romania': 'RO',
    'San Marino': 'SM',
    'Spain': 'ES',
    'Switzerland': 'CH',
    'Turkey': 'TR',
    'United Kingdom': 'GB',
    'United States': 'US',
}

# Some older venue records omit their country, but their named city is clear.
CITY_COUNTRIES = {
    'Antwerp': 'BE', 'Baku': 'AZ', 'Barcelona': 'ES', 'Basel': 'CH',
    'Berlin': 'DE', 'Budapest': 'HU', 'Copenhagen': 'DK', 'Dublin': 'IE',
    'Dugo Selo': 'HR', 'Frankfurt': 'DE', 'Genova': 'IT', 'Graz': 'AT',
    'Innsbruck': 'AT', 'Istanbul': 'TR', 'Istambul': 'TR',
    'Klagenfurt': 'AT', 'Lausanne': 'CH', 'Linz': 'AT', 'Lisboa': 'PT',
    'London': 'GB', 'Madrid': 'ES', 'Milan': 'IT', 'Oeiras': 'PT',
    'Olomouc': 'CZ', 'Prague': 'CZ', 'Roma': 'IT', 'Rom': 'IT',
    'Rome': 'IT', 'Salzburg': 'AT', 'Schwaz': 'AT', 'Southampton': 'GB',
    'Stanford': 'US', 'Sueca': 'ES', 'Timisoara': 'RO', 'Trieste': 'IT',
    'Trento': 'IT', 'Vienna': 'AT', 'Viitasaari': 'FI', 'Wien': 'AT',
    'Warsaw': 'PL', 'Zagreb': 'HR', 'Bludenz': 'AT',
    'Donostia - San Sebastián': 'ES', 'Erl': 'AT', 'Aveiro': 'PT',
}

INVALID_VENUES = {'Contact Form', 'Stacked Sidebar', 'Graz', 'Viena', 'San Marino'}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    english = re.search(r'\[:en\](.*?)(?=\[:(?:de|en)\]|\[:\]|$)', raw, re.I | re.S)
    if english:
        raw = english.group(1)
    if '<' in raw:
        soup = BeautifulSoup(raw, 'html.parser')
        for element in soup(['script', 'style']):
            element.decompose()
        text = soup.get_text('\n', strip=True)
    else:
        text = raw
    text = html.unescape(text)
    text = re.sub(r'\[:(?:en|de)\]', '', text, flags=re.I)
    text = text.replace('[:]', '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start_value = clean_text(event.get('start_date'))
    try:
        start = datetime.fromisoformat(start_value)
    except (TypeError, ValueError):
        return None

    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    if not city:
        city = clean_text(venue_data.get('province') or venue_data.get('stateprovince'))
    country_name = clean_text(venue_data.get('country'))
    country_code = COUNTRY_CODES.get(country_name) or CITY_COUNTRIES.get(city)

    if (
        not title or not url or not venue or not city or not country_code
        or venue in INVALID_VENUES or venue.casefold() == city.casefold()
    ):
        return None

    description = clean_text(event.get('description')) or None
    time_from = None if event.get('all_day') else start.strftime('%H:%M')
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SchallfeldensembleComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='schallfeldensemble_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        end_year = date.today().year + 10
        base_params = {
            'per_page': 50,
            'start_date': '2010-01-01 00:00:00',
            'end_date': f'{end_year}-12-31 23:59:59',
        }
        records = []
        skipped_count = 0
        page = 1
        total_pages = 1

        while page <= total_pages:
            try:
                response = session.get(
                    API_URL, params={**base_params, 'page': page}, timeout=60
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Schallfeld event API page',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if 'response' in locals() else API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            total_pages = int(payload.get('total_pages') or 1)
            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    skipped_count += 1
            page += 1

        if skipped_count:
            log_message(
                'Skipped Schallfeld events without a valid location or date',
                event='crawler_items_skipped',
                level='warning',
                record_count=skipped_count,
                error_type='IncompleteEventData',
                error_message='Required date, title, URL, venue, city, or country is missing',
            )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SchallfeldensembleComCrawler().run()


if __name__ == '__main__':
    main()
