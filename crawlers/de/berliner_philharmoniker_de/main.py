import math
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.berliner-philharmoniker.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'konzerte/kalender/')
SEARCH_URL = urljoin(
    SOURCE_URL,
    'filter/search/collections/performance_0/documents/search',
)
SOURCE = 'Berliner Philharmoniker'

# This is the browser-visible, search-only key used by the public calendar.
SEARCH_KEY = '09zNJI6igIRLJHhNB2YGwgaX0JApQYOL'
PAGE_SIZE = 250

MONTHS = {
    'januar': 1, 'january': 1, 'februar': 2, 'february': 2,
    'märz': 3, 'march': 3, 'april': 4, 'mai': 5, 'may': 5,
    'juni': 6, 'june': 6, 'juli': 7, 'july': 7, 'august': 8,
    'september': 9, 'oktober': 10, 'october': 10,
    'november': 11, 'dezember': 12, 'december': 12,
}

HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Referer': CALENDAR_URL,
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'x-typesense-api-key': SEARCH_KEY,
}

# Places without a city in the API are venues of the Stiftung Berliner
# Philharmoniker or partner venues in Berlin unless explicitly listed here.
GERMAN_PLACE_CITIES = {
    'alte oper frankfurt': 'Frankfurt am Main',
    'baden-baden': 'Baden-Baden',
    'carmen würth forum': 'Künzelsau',
    'elbphilharmonie': 'Hamburg',
    'kulturpalast dresden': 'Dresden',
    'kölner philharmonie': 'Köln',
    'isarphilharmonie': 'München',
    'philharmonie essen': 'Essen',
}

TOUR_LOCATIONS = {
    'aichi prefectural arts theater': ('Nagoya', 'JP'),
    'carnegie hall': ('New York', 'US'),
    'chicago symphony center': ('Chicago', 'US'),
    'dr koncerthuset': ('Kopenhagen', 'DK'),
    'felsenreitschule': ('Salzburg', 'AT'),
    'göteborgs konserthus': ('Göteborg', 'SE'),
    'grosses festspielhaus': ('Salzburg', 'AT'),
    'het concertgebouw': ('Amsterdam', 'NL'),
    'hill auditorium': ('Ann Arbor', 'US'),
    'kawaguchiko stellar theater': ('Kawaguchiko', 'JP'),
    'konserthuset, stockholm': ('Stockholm', 'SE'),
    'kultur- und kongresszentrum luzern': ('Luzern', 'CH'),
    'minato mirai hall': ('Yokohama', 'JP'),
    'mozarteum': ('Salzburg', 'AT'),
    'musikhuset aarhus': ('Aarhus', 'DK'),
    'musikverein': ('Wien', 'AT'),
    'muza kawasaki': ('Kawasaki', 'JP'),
    'national concert hall, taipeh': ('Taipeh', 'TW'),
    'osaka expo': ('Osaka', 'JP'),
    'paladozza': ('Bologna', 'IT'),
    'palais des beaux-arts': ('Brüssel', 'BE'),
    'philharmonie luxembourg': ('Luxemburg', 'LU'),
    'philharmonie de paris': ('Paris', 'FR'),
    'royal albert hall': ('London', 'GB'),
    'sala são paulo': ('São Paulo', 'BR'),
    'schloss esterházy': ('Eisenstadt', 'AT'),
    'seoul arts center': ('Seoul', 'KR'),
    'shanghai oriental art center': ('Shanghai', 'CN'),
    'solitär, mozarteum': ('Salzburg', 'AT'),
    'suntory hall': ('Tokio', 'JP'),
    'symphony hall, boston': ('Boston', 'US'),
    'szene salzburg': ('Salzburg', 'AT'),
    'teatro colón': ('Buenos Aires', 'AR'),
    'teatro mayor julio mario santo domingo': ('Bogotá', 'CO'),
    'teatro petruzzelli': ('Bari', 'IT'),
    'the john f. kennedy center': ('Washington', 'US'),
    'turku music centre': ('Turku', 'FI'),
    'usher hall': ('Edinburgh', 'GB'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def resolve_location(place, is_tour=False):
    venue = clean_text(place)
    if not venue:
        return None
    normalized = venue.casefold()

    for marker, (city, country_code) in TOUR_LOCATIONS.items():
        if marker in normalized:
            return venue, city, country_code
    for marker, city in GERMAN_PLACE_CITIES.items():
        if marker in normalized:
            return venue, city, 'DE'

    # The remaining calendar locations are named Berlin venues or rooms in
    # the Philharmonie complex. Explicit city names remain authoritative.
    if ', berlin' in normalized or ' berlin' in normalized:
        return venue, 'Berlin', 'DE'
    if is_tour:
        return None
    return venue, 'Berlin', 'DE'


def description_for(document):
    sections = []
    works = clean_text(document.get('works_formatted') or document.get('works_raw'))
    if works:
        sections.append('Programm\n' + works)

    artists = []
    for artist in document.get('artists') or []:
        name = clean_text(artist.get('name'))
        role = clean_text(artist.get('role'))
        if name:
            artists.append(f'{name} – {role}' if role else name)
    if artists:
        sections.append('Mitwirkende\n' + '\n'.join(artists))

    return '\n\n'.join(sections) or None


def parse_date(value):
    match = re.search(r'(\d{1,2})\.\s+([A-Za-zÄÖÜäöü]+)\s+(\d{4})', value or '')
    if not match:
        return None
    month = MONTHS.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def make_record(document):
    title = clean_text(document.get('title'))
    detail_url = clean_text(document.get('detail_url'))
    tags = document.get('tags') or []
    location = resolve_location(document.get('place'), is_tour='On tour' in tags)
    event_date = parse_date(document.get('date_string'))
    time_match = re.search(r'(\d{1,2})[.:](\d{2})', document.get('time_string') or '')
    if not title or not detail_url or not location or not event_date:
        return None

    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, detail_url),
        'time_from': (
            f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
            if time_match else None
        ),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_for(document),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_page(session, page):
    params = {
        'q': '',
        'query_by': (
            'title,place,works_raw,artists_raw,super_title,'
            'brand_title,brand_title_second'
        ),
        'filter_by': 'is_guest_event:false && tags:!=Führungen',
        'sort_by': 'time_start:asc',
        'drop_tokens_threshold': 0,
        'per_page': PAGE_SIZE,
        'page': page,
    }
    response = session.get(SEARCH_URL, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_page = fetch_page(session, 1)
    found = int(first_page.get('found') or 0)
    page_count = math.ceil(found / PAGE_SIZE)
    hits = list(first_page.get('hits') or [])

    for page in range(2, page_count + 1):
        try:
            hits.extend(fetch_page(session, page).get('hits') or [])
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape calendar page',
                event='crawler_page_failed',
                level='warning',
                url=SEARCH_URL,
                page=page,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    records = []
    for hit in hits:
        record = make_record(hit.get('document') or {})
        if record:
            records.append(record)
    return records


class BerlinerPhilharmonikerDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='berliner_philharmoniker_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    BerlinerPhilharmonikerDeCrawler().run()


if __name__ == '__main__':
    main()
