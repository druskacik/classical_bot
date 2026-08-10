import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ensembleresonanz.com/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Ensemble Resonanz'
EVENT_PATH = '/termine-und-tickets/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

# The calendar includes the ensemble's international guest appearances.  Most
# event headings explicitly append a city; these aliases cover halls where the
# city is instead part of the venue name or omitted as locally understood.
LOCATION_ALIASES = {
    'amare': ('Den Haag', 'NL'),
    'albertinen-haus': ('Hamburg', 'DE'),
    'amsterdam': ('Amsterdam', 'NL'),
    'antwerpen': ('Antwerpen', 'BE'),
    'atrium bonn': ('Bonn', 'DE'),
    'barbican': ('London', 'GB'),
    'barrowland': ('Glasgow', 'GB'),
    'berlin': ('Berlin', 'DE'),
    'bielefeld': ('Bielefeld', 'DE'),
    'bozar': ('Brüssel', 'BE'),
    'brüssel': ('Brüssel', 'BE'),
    'coesfeld': ('Coesfeld', 'DE'),
    'donaueschingen': ('Donaueschingen', 'DE'),
    'doelen': ('Rotterdam', 'NL'),
    'dortmund': ('Dortmund', 'DE'),
    'elbphilharmonie': ('Hamburg', 'DE'),
    'essen': ('Essen', 'DE'),
    'fabrik': ('Hamburg', 'DE'),
    'festspielhaus hellerau': ('Dresden', 'DE'),
    'festspielscheune ulrichshusen': ('Ulrichshusen', 'DE'),
    'freie akademie der künste': ('Hamburg', 'DE'),
    'friedrich-ebert-halle': ('Hamburg', 'DE'),
    'hamburg': ('Hamburg', 'DE'),
    'huddersfield': ('Huddersfield', 'GB'),
    'hanseatische materialverwaltung': ('Hamburg', 'DE'),
    'harburger theater': ('Hamburg', 'DE'),
    'jeddah': ('Dschidda', 'SA'),
    'kammermusiksaal': ('Berlin', 'DE'),
    'kampnagel': ('Hamburg', 'DE'),
    'kiel': ('Kiel', 'DE'),
    'knust': ('Hamburg', 'DE'),
    'köln': ('Köln', 'DE'),
    'kölner': ('Köln', 'DE'),
    'kronberg': ('Kronberg', 'DE'),
    'laeiszhalle': ('Hamburg', 'DE'),
    'leverkusen': ('Leverkusen', 'DE'),
    'mozarteum': ('Salzburg', 'AT'),
    'mojo club': ('Hamburg', 'DE'),
    'matthias-claudius-heim': ('Hamburg', 'DE'),
    'muziekgebouw': ('Amsterdam', 'NL'),
    'nikolaisaal': ('Potsdam', 'DE'),
    'nürnberg': ('Nürnberg', 'DE'),
    'potsdam': ('Potsdam', 'DE'),
    'pierre boulez saal': ('Berlin', 'DE'),
    'prinzregententheater': ('München', 'DE'),
    'grand théâtre de provence': ('Aix-en-Provence', 'FR'),
    'ehemaliger leitstand des bunkers': ('Hamburg', 'DE'),
    'resonanzraum': ('Hamburg', 'DE'),
    'royal albert hall': ('London', 'GB'),
    'salzburg': ('Salzburg', 'AT'),
    'szene': ('Salzburg', 'AT'),
    'schauspielhaus': ('Hamburg', 'DE'),
    'st katharinen': ('Hamburg', 'DE'),
    "st. luke's": ('Glasgow', 'GB'),
    'service wohnen für senioren': ('Hamburg', 'DE'),
    'seniorenwohnanlage wilhelm carstens': ('Hamburg', 'DE'),
    'telekom forum': ('Bonn', 'DE'),
    'thalia': ('Hamburg', 'DE'),
    'theater des westens': ('Berlin', 'DE'),
    'universität bonn': ('Bonn', 'DE'),
    'viktoriabad bonn': ('Bonn', 'DE'),
    'weimarhalle': ('Weimar', 'DE'),
    'wien': ('Wien', 'AT'),
    'wiese': ('Hamburg', 'DE'),
    'zinnschmelze': ('Hamburg', 'DE'),
}

COUNTRY_BY_CITY = {
    'Amsterdam': 'NL', 'Antwerpen': 'BE', 'Augsburg': 'DE',
    'Bad Elster': 'DE', 'Bielefeld': 'DE', 'Bonn': 'DE', 'Brugge': 'BE',
    'Brüssel': 'BE', 'Copenhagen': 'DK', 'Coesfeld': 'DE', 'Den Haag': 'NL',
    'Donaueschingen': 'DE', 'Dortmund': 'DE', 'Düsseldorf': 'DE',
    'Eindhoven': 'NL', 'Erfurt': 'DE', 'Essen': 'DE', 'Esslingen': 'DE',
    'Glasgow': 'GB', 'Hagen am Teutoburger Wald': 'DE', 'Hamburg': 'DE',
    'Hannover': 'DE', 'Heidelberg': 'DE',
    'Helmstedt': 'DE', 'Huddersfield': 'GB', 'Kempen': 'DE', 'Kiel': 'DE',
    'Köln': 'DE', 'Kronberg': 'DE', 'Künzelsau': 'DE', 'Leverkusen': 'DE',
    'London': 'GB', 'Lübeck': 'DE', 'Mannheim': 'DE', 'Monheim': 'DE',
    'Meiningen': 'DE', 'Nürnberg': 'DE', 'Osnabrück': 'DE', 'Perugia': 'IT',
    'Potsdam': 'DE', 'Rendsburg-Büdelsdorf': 'DE', 'Rothenburg': 'DE',
    'Salzburg': 'AT', 'Schwetzingen': 'DE',
    'Straßburg': 'FR', 'Strasbourg': 'FR', 'Stuttgart': 'DE', 'Utrecht': 'NL',
    'Viersen': 'DE', 'Wernigerode': 'DE', 'Wien': 'AT', 'Wiesloch': 'DE',
    'Witten': 'DE', 'Wolfsburg': 'DE', 'Würzburg': 'DE', 'Berlin': 'DE',
    'Bremen': 'DE', 'Trier': 'DE', 'Ulrichshusen': 'DE',
    'München': 'DE', 'Paris': 'FR', 'Luzern': 'CH', 'Basel': 'CH',
}

DATE_RE = re.compile(r'(\d{2})\.(\d{2})\.(\d{4})')
TIME_RE = re.compile(r'(\d{1,2}):(\d{2})\s*Uhr', re.I)
URL_DATE_RE = re.compile(r'-20\d{2}-\d{2}-\d{2}(?:-\d+)?/?$')


def clean_text(value):
    if not value:
        return ''
    value = str(value)
    text = (
        BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
        if '<' in value else value
    )
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def event_urls(session):
    soup = BeautifulSoup(get(session, SITEMAP_URL).text, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        if EVENT_PATH not in url or '/en/' in url or not URL_DATE_RE.search(url):
            continue
        urls.append(url.rstrip('/'))
    return list(dict.fromkeys(urls))


def resolve_location(venue_text):
    venue_text = clean_text(venue_text)
    if not venue_text:
        return None

    lowered = venue_text.casefold()
    for alias, (city, country_code) in LOCATION_ALIASES.items():
        if alias in lowered:
            return venue_text, city, country_code

    for city, country_code in COUNTRY_BY_CITY.items():
        if city.casefold() in lowered:
            return venue_text, city, country_code

    # A large portion of touring dates use the stable "Hall, City" format.
    if ',' in venue_text:
        candidate = clean_text(venue_text.rsplit(',', 1)[1])
        country_code = COUNTRY_BY_CITY.get(candidate)
        if country_code:
            return venue_text, candidate, country_code
    return None


def parse_event(session, url):
    soup = BeautifulSoup(get(session, url).text, 'html.parser')
    event = soup.select_one('.single-event')
    if not event:
        return None

    title = clean_text(event.select_one('h1'))
    header = event.select_one('header .text-p1')
    header_parts = list(header.stripped_strings) if header else []
    if not title or len(header_parts) < 2:
        return None

    date_match = DATE_RE.search(header_parts[0])
    time_match = TIME_RE.search(header_parts[0])
    location = resolve_location(header_parts[1])
    if not date_match or not location:
        return None
    day, month, year = map(int, date_match.groups())
    try:
        event_date = date(year, month, day).isoformat()
    except ValueError:
        return None

    venue, city, country_code = location
    description_node = event.select_one('.mt-l.text-p1')
    description = clean_text(description_node)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': (
            f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
            if time_match else None
        ),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(parse_event, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class EnsembleResonanzComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensembleresonanz_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    EnsembleResonanzComCrawler().run()


if __name__ == '__main__':
    main()
