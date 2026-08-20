import html
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = 'Christoph Eschenbach'
SOURCE_URL = 'https://christopheschenbach.com/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
PAGE_SIZE = 50

# The artist tours internationally.  The Events Calendar entries contain an
# address, but newer entries do not use its structured venue fields, so these
# place names provide a conservative fallback for the cities present in the
# calendar. Unknown locations are skipped rather than assigned a wrong country.
CITY_COUNTRIES = {
    'aix-en-provence': ('Aix-en-Provence', 'FR'),
    'athens': ('Athens', 'GR'),
    'baden-baden': ('Baden-Baden', 'DE'),
    'bamberg': ('Bamberg', 'DE'),
    'barcelona': ('Barcelona', 'ES'),
    'berlin': ('Berlin', 'DE'),
    'budapest': ('Budapest', 'HU'),
    'chicago': ('Chicago', 'US'),
    'dresden': ('Dresden', 'DE'),
    'düsseldorf': ('Düsseldorf', 'DE'),
    'duesseldorf': ('Düsseldorf', 'DE'),
    'flensburg': ('Flensburg', 'DE'),
    'freiburg': ('Freiburg', 'DE'),
    'granada': ('Granada', 'ES'),
    'haifa': ('Haifa', 'IL'),
    'hamburg': ('Hamburg', 'DE'),
    'hong kong': ('Hong Kong', 'HK'),
    'houston': ('Houston', 'US'),
    'innsbruck': ('Innsbruck', 'AT'),
    'istanbul': ('Istanbul', 'TR'),
    'jerusalem': ('Jerusalem', 'IL'),
    'kiel': ('Kiel', 'DE'),
    'lausanne': ('Lausanne', 'CH'),
    'london': ('London', 'GB'),
    'luzern': ('Luzern', 'CH'),
    'lübeck': ('Lübeck', 'DE'),
    'madrid': ('Madrid', 'ES'),
    'matsumoto': ('Matsumoto', 'JP'),
    'new york': ('New York', 'US'),
    'osaka': ('Osaka', 'JP'),
    'oxford': ('Oxford', 'GB'),
    'paris': ('Paris', 'FR'),
    'parma': ('Parma', 'IT'),
    'philadelphia': ('Philadelphia', 'US'),
    'pittsburgh': ('Pittsburgh', 'US'),
    'rendsburg': ('Rendsburg', 'DE'),
    'sonderburg': ('Sønderborg', 'DK'),
    'sønderborg': ('Sønderborg', 'DK'),
    'stuttgart': ('Stuttgart', 'DE'),
    'tarragona': ('Tarragona', 'ES'),
    'tel aviv': ('Tel Aviv', 'IL'),
    'tel aviv-yafo': ('Tel Aviv', 'IL'),
    'tokyo': ('Tokyo', 'JP'),
    'úbeda': ('Úbeda', 'ES'),
    'ùbeda': ('Úbeda', 'ES'),
    'ubeda': ('Úbeda', 'ES'),
    'vienna': ('Vienna', 'AT'),
    'wien': ('Vienna', 'AT'),
    'warsaw': ('Warsaw', 'PL'),
    'washington': ('Washington', 'US'),
    'wrocław': ('Wrocław', 'PL'),
    'wroclaw': ('Wrocław', 'PL'),
    'zürich': ('Zürich', 'CH'),
    'zurich': ('Zürich', 'CH'),
}

COUNTRY_MARKERS = {
    'austria': 'AT',
    'danmark': 'DK',
    'denmark': 'DK',
    'deutschland': 'DE',
    'france': 'FR',
    'germany': 'DE',
    'greece': 'GR',
    'hungary': 'HU',
    'israel': 'IL',
    'italia': 'IT',
    'italy': 'IT',
    'japan': 'JP',
    'poland': 'PL',
    'spain': 'ES',
    'switzerland': 'CH',
    'turkey': 'TR',
    'united kingdom': 'GB',
    'united states': 'US',
    'usa': 'US',
}


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def description_lines(event):
    return clean_text(event.get('description')).splitlines()


def find_location(event, lines):
    venue_data = event.get('venue')
    api_venue = ''
    api_address = ''
    api_city = ''
    api_country = ''
    if isinstance(venue_data, dict):
        api_venue = clean_text(venue_data.get('venue'))
        api_address = clean_text(venue_data.get('address'))
        api_city = clean_text(venue_data.get('city'))
        api_country = clean_text(venue_data.get('country'))

    # Calendar editors consistently put the venue and address first. Prefer
    # that current text because one historical entry has stale venue metadata.
    first_line = lines[0].strip(' ,') if lines else ''
    venue = first_line if len(first_line) > 2 else api_venue
    if api_venue and re.search(r'\b(?:17|18|19|20)\d{2}\b', venue):
        venue = api_venue
    if re.search(r'\b\d{4,6}\b', venue):
        venue = venue.split(',', 1)[0].strip()
    if venue.lower() in CITY_COUNTRIES or venue.casefold() in {
        city.casefold() for city, _ in CITY_COUNTRIES.values()
    }:
        venue = api_venue if api_venue.casefold() != venue.casefold() else ''

    location_text = '\n'.join(
        [*lines[:4], event.get('title') or '', api_city, api_address, api_country]
    )
    folded = location_text.casefold()
    city = None
    country_code = None
    # Prefer the first place mentioned, with the longer marker winning ties.
    matches = []
    for marker in CITY_COUNTRIES:
        position = folded.find(marker.casefold())
        if position >= 0:
            matches.append((position, -len(marker), marker))
    if matches:
        city, country_code = CITY_COUNTRIES[min(matches)[2]]
    if api_city and not city:
        city = api_city
    for marker, code in COUNTRY_MARKERS.items():
        if re.search(rf'(?<!\w){re.escape(marker)}(?!\w)', folded):
            country_code = code
            break

    return html.unescape(venue).strip(), city, country_code


def parse_event(event):
    title = clean_text(event.get('title'))
    url = html.unescape((event.get('url') or '').strip())
    try:
        starts_at = datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S')
    except (KeyError, TypeError, ValueError):
        return None

    lines = description_lines(event)
    venue, city, country_code = find_location(event, lines)
    if not title or not url or not venue or not city or not country_code:
        return None

    all_day = bool(event.get('all_day'))
    time_from = None if all_day else starts_at.strftime('%H:%M:%S')
    time_to = None
    if not all_day and event.get('end_date'):
        try:
            ends_at = datetime.strptime(event['end_date'], '%Y-%m-%d %H:%M:%S')
            if ends_at != starts_at:
                time_to = ends_at.strftime('%H:%M:%S')
        except (TypeError, ValueError):
            pass

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'time_to': time_to,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n'.join(lines) or None,
    }


class ChristophEschenbachCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='christopheschenbach_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({'User-Agent': 'classical-bot/1.0'})
        params = {
            'start_date': '1900-01-01',
            'end_date': f'{date.today().year + 10}-12-31',
            'per_page': PAGE_SIZE,
            'page': 1,
        }
        records = []
        total_pages = 1
        while params['page'] <= total_pages:
            try:
                response = session.get(API_URL, params=params, timeout=60)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Christoph Eschenbach calendar',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            total_pages = int(payload.get('total_pages') or 0)
            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped calendar item with incomplete required fields',
                        event='crawler_item_skipped',
                        level='warning',
                        url=html.unescape((event.get('url') or '').strip()),
                        event_id=event.get('id'),
                    )
            params['page'] += 1

        log_message(
            'Fetched Christoph Eschenbach calendar',
            event='crawler_api_completed',
            level='info',
            url=API_URL,
            record_count=len(records),
        )
        return records


def main():
    ChristophEschenbachCrawler().run()


if __name__ == '__main__':
    main()
