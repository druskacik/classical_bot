import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://johannarose.net/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')
SOURCE = 'Johanna Rose'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRIES = {
    'austria': 'AT', 'belgium': 'BE', 'canada': 'CA', 'croatia': 'HR',
    'france': 'FR', 'germany': 'DE', 'holland': 'NL', 'italy': 'IT',
    'lithuania': 'LT', 'malta': 'MT', 'morokko': 'MA', 'norway': 'NO',
    'poland': 'PL', 'polen': 'PL', 'portugal': 'PT', 'spain': 'ES',
    'sweden': 'SE', 'switzerland': 'CH', 'usa': 'US', 'us': 'US',
}

# The calendar often omits the country, and sometimes labels a festival rather
# than a hall. These are only used when the displayed place is unambiguous.
CITY_COUNTRIES = {
    'albacete': 'ES', 'almagro': 'ES', 'amsterdam': 'NL', 'antwerpen': 'BE',
    'aracena': 'ES', 'aranjuez': 'ES', 'badajoz': 'ES', 'barcelona': 'ES',
    'bayreuth': 'DE', 'berkeley': 'US', 'berlin': 'DE', 'bilbao': 'ES',
    'boston': 'US', 'calgary': 'CA', 'córdoba': 'ES', 'cordoba': 'ES',
    'cuenca': 'ES', 'deventer': 'NL', 'dubrovnik': 'HR', 'edmonton': 'CA',
    'eindhoven': 'NL', 'fürth': 'DE', 'geel': 'BE', 'granada': 'ES',
    'guimaraes': 'PT', 'hamburg': 'DE', 'helsinki': 'FI', 'köln': 'DE',
    'leon': 'ES', 'linz': 'AT', 'lodi': 'IT', 'madrid': 'ES',
    'maastricht': 'NL', 'mechelen': 'BE', 'melk': 'AT', 'montreal': 'CA',
    'murcia': 'ES', 'new york': 'US', 'pamplona': 'ES', 'pisa': 'IT',
    'potsdam': 'DE', 'québec': 'CA', 'quebéc': 'CA', 'regensburg': 'DE',
    'salamanca': 'ES', 'santander': 'ES', 'saskatoon': 'CA', 'seattle': 'US',
    'seville': 'ES', 'sevilla': 'ES', 'stuttgart': 'DE', 'toronto': 'CA',
    'trondheim': 'NO', 'utrecht': 'NL', 'valencia': 'ES', 'valletta': 'MT',
    'vancouver': 'CA', 'vicenza': 'IT', 'victoria': 'CA', 'vlissingen': 'NL',
    'washington': 'US', 'washington d.c.': 'US', 'westzaan': 'NL',
}

VENUE_WORDS = re.compile(
    r'\b(?:auditorio|brucknerhaus|church|club|dumbarton oaks|elbphilar|'
    r'espacio|first church|museum|palacio|parroquia|philharmoni|sala|teatro)\b',
    re.IGNORECASE,
)


def clean_text(element):
    if element is None:
        return ''
    return re.sub(r'\s+', ' ', element.get_text(' ', strip=True)).strip()


def parse_date(value):
    match = re.fullmatch(r'\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*', value or '')
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def city_country(component):
    normalized = component.strip().casefold()
    if normalized in CITY_COUNTRIES:
        return component.strip(), CITY_COUNTRIES[normalized]
    for city, country_code in CITY_COUNTRIES.items():
        if re.search(rf'\b{re.escape(city)}\b', normalized):
            return city.title(), country_code
    return None


def parse_location(value):
    parts = [part.strip(' .') for part in value.split(',') if part.strip(' .')]
    if not parts:
        return None

    explicit_country = None
    retained = []
    for part in parts:
        code = COUNTRIES.get(part.casefold())
        if code:
            explicit_country = code
        else:
            retained.append(part)

    venues = [part for part in retained if VENUE_WORDS.search(part)]
    cities = [city_country(part) for part in retained]
    cities = [item for item in cities if item and item[0].casefold() not in {
        venue.casefold() for venue in venues
    }]
    if not venues or not cities:
        return None

    city, inferred_country = cities[0]
    country_code = explicit_country or inferred_country
    if explicit_country and explicit_country != inferred_country:
        return None
    venue = re.sub(
        rf'^\s*{re.escape(city)}\s*[.\-:]\s*', '', venues[0], flags=re.IGNORECASE
    ).strip()
    if not venue:
        return None
    return venue, city, country_code


def parse_event(event):
    title = clean_text(event.select_one('.evcal_event_title'))
    schema = event.select_one('.evo_event_schema')
    url_element = schema.select_one('[itemprop="url"][href]') if schema else None
    date_element = schema.select_one('[itemprop="startDate"][content]') if schema else None
    event_date = parse_date(date_element.get('content')) if date_element else None
    location = parse_location(clean_text(event.select_one('.evcal_event_subtitle')))
    if not title or not url_element or not event_date or not location:
        return None

    time_element = event.select_one('.evo_time .start')
    time_match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', clean_text(time_element))
    description = clean_text(event.select_one('.eventon_desc_in')) or None
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, url_element['href']),
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class JohannaRoseNetCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='johannarose_net',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Johanna Rose calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = [
            record for event in soup.select('.eventon_list_event')
            if (record := parse_event(event)) is not None
        ]
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ))


def main():
    JohannaRoseNetCrawler().run()


if __name__ == '__main__':
    main()
