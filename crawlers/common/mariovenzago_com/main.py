import html
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mariovenzago.com/'
SOURCE = 'Mario Venzago'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'de,en;q=0.8',
}

COUNTRY_NAMES = {
    'canada': 'CA',
    'dänemark': 'DK',
    'deutschland': 'DE',
    'estland': 'EE',
    'germany': 'DE',
    'italien': 'IT',
    'italy': 'IT',
    'japan': 'JP',
    'korea': 'KR',
    'poland': 'PL',
    'schweden': 'SE',
    'schweiz': 'CH',
    'singapur': 'SG',
    'taiwan': 'TW',
    'usa': 'US',
    'vereinigte staaten von amerika': 'US',
}

COUNTRY_MARKERS = {
    'A': 'AT', 'CAN': 'CA', 'CH': 'CH', 'D': 'DE', 'DK': 'DK',
    'EST': 'EE', 'FI': 'FI', 'IT': 'IT', 'J': 'JP', 'JP': 'JP',
    'KR': 'KR', 'PL': 'PL', 'SE': 'SE', 'SG': 'SG', 'SGP': 'SG',
    'TW': 'TW', 'US': 'US', 'USA': 'US',
}

# These are the few API venue labels which do not use the site's usual
# "City (country) | Hall" format but still identify both values unambiguously.
SPECIAL_VENUES = {
    'Berlin Konzertsaal der Unversität der Künste (UdK)': (
        'Berlin', 'Konzertsaal der Universität der Künste (UdK)'
    ),
    'Miaobei Art Center (TW)': ('Miaobei', 'Miaobei Art Center'),
    'Tampere Hall Main Auditorium (FI)': ('Tampere', 'Tampere Hall Main Auditorium'),
    'Tokio (J) Suntory Hall': ('Tokio', 'Suntory Hall'),
    'Ulm Kornhaus': ('Ulm', 'Kornhaus'),
    'Weikersheim – Tauberphilharmonie – Venue': ('Weikersheim', 'Tauberphilharmonie'),
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    text = html.unescape(soup.get_text('\n', strip=True))
    return re.sub(r'[ \t\r\f\v]+', ' ', text).strip()


def country_code(event, venue_name):
    country = clean_text((event.get('venue') or {}).get('country')).casefold()
    if country in COUNTRY_NAMES:
        return COUNTRY_NAMES[country]

    markers = re.findall(r'\(([^()]*)\)', venue_name)
    for marker in reversed(markers):
        normalized = marker.strip().upper()
        if normalized in COUNTRY_MARKERS:
            return COUNTRY_MARKERS[normalized]
    return None


def city_and_venue(venue_name):
    venue_name = clean_text(venue_name)
    if venue_name in SPECIAL_VENUES:
        return SPECIAL_VENUES[venue_name]

    if '|' not in venue_name:
        return None, None

    city_part, hall = (part.strip(' ,') for part in venue_name.split('|', 1))
    city = re.sub(
        r'\s*\(\s*(?:A|CAN|CH|D|DK|EST|FI|IT|J|JP|KR|PL|SE|SGP?|TW|USA?)\s*\)?\s*$',
        '',
        city_part,
    )
    hall = re.sub(r',\s*(?:Canada|Deutschland|Germany|Italy|Poland|Taiwan)\s*$', '', hall, flags=re.I)
    city = city.strip(' ,')
    hall = hall.strip(' ,')
    return (city, hall) if city and hall else (None, None)


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    venue_data = event.get('venue') or {}
    raw_venue = clean_text(venue_data.get('venue'))
    city, venue = city_and_venue(raw_venue)
    code = country_code(event, raw_venue)

    try:
        event_date = date.fromisoformat(str(event.get('start_date', ''))[:10]).isoformat()
    except ValueError:
        return None

    time_from = None
    if not event.get('all_day'):
        try:
            time_from = datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S').strftime('%H:%M')
        except (KeyError, TypeError, ValueError):
            pass

    if not all((title, url, event_date, venue, city, code)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MarioVenzagoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mariovenzago_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'per_page': 50,
            'start_date': '1900-01-01',
            'end_date': '2100-12-31',
            'page': 1,
        }
        records = []
        skipped_count = 0

        while True:
            try:
                response = session.get(API_URL, params=params, timeout=60)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Mario Venzago events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    skipped_count += 1

            total_pages = int(payload.get('total_pages') or 1)
            if params['page'] >= total_pages:
                break
            params['page'] += 1

        if skipped_count:
            log_message(
                'Skipped Mario Venzago events with incomplete location data',
                event='crawler_records_skipped',
                level='warning',
                record_count=skipped_count,
            )
        return records


def main():
    return MarioVenzagoComCrawler().run()


if __name__ == '__main__':
    main()
