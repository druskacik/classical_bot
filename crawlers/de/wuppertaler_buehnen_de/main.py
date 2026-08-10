import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wuppertaler-buehnen.de/'
SOURCE = 'Wuppertaler Bühnen – Sinfonieorchester Wuppertal'
API_ORIGIN = 'https://api.wb.c4c.it'
API_URL = f'{API_ORIGIN}/api'

HEADERS = {
    'Accept': 'application/ld+json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

# The orchestra mainly performs in Wuppertal, but its archive also includes
# tours. Locations not listed here are only defaulted when their name itself
# identifies Wuppertal.
LOCATION_GEOGRAPHY = {
    'Altenberger Dom': ('Odenthal', 'DE'),
    'Anneliese Brost Musikforum Ruhr': ('Bochum', 'DE'),
    'Concertgebouw Amsterdam': ('Amsterdam', 'NL'),
    'Centro Cultural de Belém': ('Lisboa', 'PT'),
    'Evangelische Kirche Herzkamp': ('Sprockhövel', 'DE'),
    'Festhalle Viersen': ('Viersen', 'DE'),
    'Großes Festspielhaus Salzburg': ('Salzburg', 'AT'),
    'Henrichshütte Hattingen, Gebläsehalle': ('Hattingen', 'DE'),
    'Konzert Theater Coesfeld': ('Coesfeld', 'DE'),
    'Kölner Philharmonie': ('Köln', 'DE'),
    'LWL-Industriemuseum Henrichshütte Hattingen': ('Hattingen', 'DE'),
    'Marienburgpark, Monheim am Rhein': ('Monheim am Rhein', 'DE'),
    'Salzlager der Kokerei Zollverein': ('Essen', 'DE'),
}

WUPPERTAL_LOCATIONS = {
    'Basilika St. Laurentius, Friedrich-Ebert-Str. 22',
    'CityKirche Elberfeld',
    'codeks Arena',
    'CVJM Langerfeld',
    'Friedhofskirche Wuppertal',
    'Historische Stadthalle Wuppertal',
    'Immanuelskirche Wuppertal',
    'INSEL',
    'Johannes-Rau-Platz',
    'Kronleuchterfoyer Opernhaus',
    'Laurentiusplatz',
    'Nachbarschaftsheim Wuppertal e. V.',
    'Opernhaus',
    'Permakulturhof',
    'Platz der Republik',
    'Skulpturenpark Waldfrieden',
    'Thomaskirche Wuppertal',
    'Unterbarmer Hauptkirche',
    'Vereinsheim des CVJM Wuppertal-Langerfeld e.V.',
    'WSW Bus-Betriebshof Varresbeck',
    'CinemaxX Wuppertal',
}


def clean_html(value):
    if not value:
        return None
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def geography_for(venue):
    venue = re.sub(r'\s+', ' ', (venue or '')).strip()
    if not venue:
        return None
    if venue in LOCATION_GEOGRAPHY:
        return venue, *LOCATION_GEOGRAPHY[venue]
    if venue in WUPPERTAL_LOCATIONS or 'wuppertal' in venue.lower():
        return venue, 'Wuppertal', 'DE'
    return None


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def list_performances(session):
    params = {
        'groups[]': 'list',
        'production.branch.id[]': 3,
        'order[startDateTime]': 'ASC',
        'page': 1,
    }
    performances = []
    while True:
        payload = get_json(session, f'{API_URL}/performances', params=params)
        performances.extend(payload.get('hydra:member', []))
        if not payload.get('hydra:view', {}).get('hydra:next'):
            break
        params['page'] += 1
    return performances


def get_description(session, production_id):
    payload = get_json(session, f'{API_ORIGIN}{production_id}')
    return clean_html(payload.get('description') or payload.get('shortDescription'))


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    performances = list_performances(session)

    production_ids = {
        item.get('production', {}).get('@id')
        for item in performances
        if item.get('production', {}).get('@id')
    }
    descriptions = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_description, session, production_id): production_id
            for production_id in production_ids
        }
        for future in as_completed(futures):
            production_id = futures[future]
            try:
                descriptions[production_id] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape production detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=f'{API_ORIGIN}{production_id}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for item in performances:
        production = item.get('production') or {}
        title = re.sub(r'\s+', ' ', production.get('title') or '').strip()
        url = (item.get('url') or '').strip()
        location = geography_for((item.get('location') or {}).get('title'))
        try:
            starts_at = datetime.fromisoformat(item['startDateTime'])
        except (KeyError, TypeError, ValueError):
            continue
        if not title or not url or not location:
            continue
        venue, city, country_code = location
        records.append({
            'title': title,
            'date': starts_at.date().isoformat(),
            'url': url,
            'time_from': starts_at.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': descriptions.get(production.get('@id'))
            or clean_html(production.get('shortDescription')),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class WuppertalerBuehnenDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wuppertaler_buehnen_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    WuppertalerBuehnenDeCrawler().run()


if __name__ == '__main__':
    main()
