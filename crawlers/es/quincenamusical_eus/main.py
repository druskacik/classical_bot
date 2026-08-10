import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.quincenamusical.eus/es/'
PROGRAM_URL = urljoin(SOURCE_URL, 'programa')
SOURCE = 'Quincena Musical de San Sebastián'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}

# Venues in the home city are commonly shown without a city. Touring venues
# generally include their municipality in parentheses, which takes priority.
HOME_VENUES = (
    'auditorio kursaal',
    'teatro victoria eugenia',
    'museo san telmo',
    'tabakalera',
    'iglesia de san vicente',
    'iglesia de iesu',
    'basílica de santa maría',
    'basilica de santa maria',
    'convento de santa teresa',
    'palacio miramar',
)

CITY_ALIASES = {
    'donostia': ('San Sebastián', 'ES'),
    'san sebastián': ('San Sebastián', 'ES'),
    'san sebastian': ('San Sebastián', 'ES'),
    'vitoria-gasteiz': ('Vitoria-Gasteiz', 'ES'),
    'vitoria': ('Vitoria-Gasteiz', 'ES'),
    'gasteiz': ('Vitoria-Gasteiz', 'ES'),
    'saint-pée-sur-nivelle': ('Saint-Pée-sur-Nivelle', 'FR'),
    'saint pee sur nivelle': ('Saint-Pée-sur-Nivelle', 'FR'),
    'senpere': ('Saint-Pée-sur-Nivelle', 'FR'),
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = re.search(r'\b(\d{2}\.\d{2}\.\d{4})(?:\s*-\s*(\d{1,2}:\d{2}))?', value)
    if not match:
        return None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None
    return event_date, match.group(2)


def resolve_location(venue):
    normalized = venue.casefold()
    if 'san sebastián' in normalized or 'san sebastian' in normalized:
        return 'San Sebastián', 'ES'
    if 'ategorrieta' in normalized or any(marker in normalized for marker in HOME_VENUES):
        return 'San Sebastián', 'ES'

    parenthetical = re.search(r'\(([^()]*)\)\s*$', venue)
    if parenthetical:
        named_city = parenthetical.group(1).strip()
        alias = CITY_ALIASES.get(named_city.casefold())
        if alias:
            return alias
        # The festival's Spanish touring venues consistently put the
        # municipality in a final parenthetical.
        if named_city and not re.search(
            r'\d|:|iglesia|auditorio|teatro|acceso|patio|sala|hall|espacios|museo',
            named_city,
            re.I,
        ):
            return named_city, 'ES'

    for marker, location in CITY_ALIASES.items():
        if marker in normalized:
            return location
    return None


def parse_event(response, url):
    soup = BeautifulSoup(response.text, 'html.parser')
    title = clean_text(soup.select_one('.datos h1[itemprop="headline"], .datos h1'))
    date_and_time = parse_datetime(clean_text(soup.select_one('.datos .fecha, .ficha .fecha')))
    venue = clean_text(soup.select_one('.datos .sede, .ficha .sede'))
    location = resolve_location(venue) if venue else None
    if not title or not date_and_time or not venue or not location:
        return None

    description_parts = []
    for section in soup.select('.cuerpo .textos.descripcion, .cuerpo .textos.programa'):
        text = clean_text(section)
        if text and text not in description_parts:
            description_parts.append(text)

    event_date, event_time = date_and_time
    city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class QuincenaMusicalEusCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='quincenamusical_eus',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='potential',
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
            response = session.get(PROGRAM_URL, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Quincena Musical programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAM_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        urls = sorted({
            urljoin(PROGRAM_URL, link['href'])
            for link in soup.select('a[href*="/es/evento/"]')
        })
        records = []

        def fetch_event(url):
            detail_response = session.get(url, timeout=60)
            detail_response.raise_for_status()
            return parse_event(detail_response, url)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Quincena Musical event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    QuincenaMusicalEusCrawler().run()


if __name__ == '__main__':
    main()
