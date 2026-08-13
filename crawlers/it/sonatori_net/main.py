import re
import unicodedata
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sonatori.net/'
CALENDAR_URL = urljoin(SOURCE_URL, 'ensemble/concerti.html')
SOURCE = 'Sonatori de la Gioiosa Marca'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}

# The ensemble tours internationally. These first-party location labels either
# name a city directly or identify a well-known city-specific venue/festival.
LOCATION_HINTS = {
    'wigmore hall': ('London', 'GB'),
    'potsdam sanssouci': ('Potsdam', 'DE'),
    'beethovenfest': ('Bonn', 'DE'),
    'dusseldorf festival': ('Düsseldorf', 'DE'),
    'muhlhausen': ('Mühlhausen', 'DE'),
    'styriarte': ('Graz', 'AT'),
    'palazzo dei pio': ('Carpi', 'IT'),
    'radovljica': ('Radovljica', 'SI'),
    'enescu festival': ('Bucharest', 'RO'),
    'bukarest': ('Bucharest', 'RO'),
    'maulbronn': ('Maulbronn', 'DE'),
    'bologna': ('Bologna', 'IT'),
    'verona': ('Verona', 'IT'),
    'treviso': ('Treviso', 'IT'),
    'trieste': ('Trieste', 'IT'),
    'roma': ('Roma', 'IT'),
    'ancona': ('Ancona', 'IT'),
    'monfalcone': ('Monfalcone', 'IT'),
    'sassari': ('Sassari', 'IT'),
    'san vito al tagliamento': ('San Vito al Tagliamento', 'IT'),
    'venezia': ('Venezia', 'IT'),
    'berlino': ('Berlin', 'DE'),
    'brugine': ('Brugine', 'IT'),
    'san pietro di feletto': ('San Pietro di Feletto', 'IT'),
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    value = unicodedata.normalize('NFKD', value.casefold())
    return ''.join(character for character in value if not unicodedata.combining(character))


def parse_location(value):
    venue = re.sub(r'\s+(?:POSTPONED|CANCELLED|ANNULLATO)\s*$', '', value, flags=re.I).strip()
    key = normalized(venue)
    for hint, location in LOCATION_HINTS.items():
        if hint in key:
            return venue, *location

    # The calendar frequently uses "venue | city" for new Italian dates.
    if '|' in venue:
        possible_city = venue.rsplit('|', 1)[1].strip()
        possible_city = re.sub(r'\s*\([A-Z]{2}\)\s*$', '', possible_city).strip()
        if possible_city and not re.search(r'\b(teatro|sala|chiesa|auditorium)\b', possible_city, re.I):
            return venue, possible_city, 'IT'
    return None


def parse_date_time(value):
    match = re.search(
        r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})(?:[, ]+([0-2]?\d[:.]\d{2}))?',
        value,
    )
    if not match:
        return None
    try:
        event_date = datetime(
            int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1))
        ).date().isoformat()
    except (KeyError, ValueError):
        return None
    time_from = match.group(4).replace('.', ':') if match.group(4) else None
    if time_from and len(time_from) == 4:
        time_from = f'0{time_from}'
    return event_date, time_from


def detail_fields(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    body = soup.select_one('#jevents_body')
    if not body:
        return None, None, None

    metadata = body.select_one('.jev_eventdetails_body')
    metadata_text = clean_text(metadata)
    location_match = re.search(r'Luogo\s*:\s*(.+?)(?:\n|Categoria\s*:|$)', metadata_text, re.I)
    when_match = re.search(r'Quando\s*:\s*(.+?)(?:\n|Luogo\s*:|$)', metadata_text, re.I)

    bodies = body.select('.jev_eventdetails_body')
    description = clean_text(bodies[1]) if len(bodies) > 1 else None
    return (
        clean_text(location_match.group(1)) if location_match else None,
        clean_text(when_match.group(1)) if when_match else None,
        description or None,
    )


class SonatoriNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sonatori_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CALENDAR_URL, timeout=45)
            response.raise_for_status()
            calendar = BeautifulSoup(response.content, 'html.parser')
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Sonatori concert calendar',
                event='crawler_fetch_failed', level='error', url=CALENDAR_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        seen_urls = set()
        for link in calendar.select('a.ev_link_row[href]'):
            url = urljoin(CALENDAR_URL, link.get('href', ''))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = clean_text(link)
            try:
                venue_text, when_text, description = detail_fields(session, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Sonatori concert detail',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue

            parsed_when = parse_date_time(when_text or '')
            parsed_location = parse_location(venue_text or '')
            if not title or not parsed_when or not parsed_location:
                log_message(
                    'Skipping Sonatori concert with incomplete date or location',
                    event='crawler_item_skipped', level='warning', url=url,
                )
                continue
            event_date, time_from = parsed_when
            venue, city, country_code = parsed_location
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    SonatoriNetCrawler().run()


if __name__ == '__main__':
    main()
