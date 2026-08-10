import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.oviedofilarmonia.es/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda/')
SOURCE = 'Oviedo Filarmonía'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11,
    'diciembre': 12,
}

# The orchestra's calendar is based in Oviedo but also includes tours. These
# explicit names prevent a touring performance from inheriting its home city.
CITY_MARKERS = {
    'gijón': 'Gijón',
    'aviles': 'Avilés',
    'avilés': 'Avilés',
    'santander': 'Santander',
    'madrid': 'Madrid',
    'bilbao': 'Bilbao',
    'león': 'León',
    'a coruña': 'A Coruña',
    'la coruña': 'A Coruña',
    'pamplona': 'Pamplona',
}

VENUE_CITIES = {
    'teatro jovellanos': 'Gijón',
    'palacio de festivales de cantabria': 'Santander',
    'auditorio nacional': 'Madrid',
    'teatro real': 'Madrid',
    'palacio euskalduna': 'Bilbao',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(value):
    match = re.search(
        r'\b(\d{1,2})\s+de\s+([a-záéíóúüñ]+)\s+de\s+(20\d{2})\b',
        clean_text(value).casefold(),
    )
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def venue_and_time(value):
    text = clean_text(value)
    time_match = re.search(
        r'(?<!\d)([01]?\d|2[0-3])(?:[.:]([0-5]\d))?\s*(?:h(?:oras?)?)?\s*$',
        text,
        re.IGNORECASE,
    )
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2) or "00"}'
        venue = text[:time_match.start()].rstrip(' ,.-')
    else:
        venue = text.strip(' ,.-')

    # These are descriptions of a multi-location event, not venue names.
    if re.match(r'la\s+ofil\s+ofrece\b', venue, re.IGNORECASE):
        return None, time_from
    return venue or None, time_from


def city_for(venue):
    folded = venue.casefold()
    for marker, city in CITY_MARKERS.items():
        if re.search(rf'\b{re.escape(marker)}\b', folded):
            return city
    for marker, city in VENUE_CITIES.items():
        if marker in folded:
            return city
    return 'Oviedo'


def is_agenda_page(url):
    parsed = urlparse(url)
    if parsed.netloc not in ('oviedofilarmonia.es', 'www.oviedofilarmonia.es'):
        return False
    return bool(
        re.fullmatch(r'/agenda/(?:\d+/)?', parsed.path)
        or (parsed.path == '/agenda.php' and 'temporada=' in parsed.query)
    )


def discover_listing_pages(session):
    """Discover current pagination and every season archive linked by the site."""
    pending = [AGENDA_URL]
    seen = set()
    soups = []
    while pending:
        url = pending.pop()
        if url in seen:
            continue
        seen.add(url)
        soup = get_soup(session, url)
        soups.append((url, soup))
        for link in soup.select('a[href]'):
            candidate = urljoin(SOURCE_URL, link.get('href'))
            if is_agenda_page(candidate) and candidate not in seen:
                pending.append(candidate)
    return soups


def listing_records(soup):
    records = []
    for title_node in soup.select('.notTit'):
        link = title_node.select_one('a[href*="agenda-ofil-"]')
        container = title_node.parent
        date_node = container.select_one('.notFecha')
        venue_node = container.select_one('.notSub')
        if not link or not date_node or not venue_node:
            continue
        title = clean_text(link)
        event_date = parse_date(date_node)
        venue, time_from = venue_and_time(venue_node)
        if not all((title, event_date, venue)):
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': urljoin(SOURCE_URL, link.get('href')),
            'time_from': time_from,
            'venue': venue,
            'city': city_for(venue),
            'country_code': 'ES',
            'description': clean_text(container.select_one('.notTxt')) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def detail_description(session, url):
    soup = get_soup(session, url)
    return clean_text(soup.select_one('#columna_izquierda .notTxt')) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for _, soup in discover_listing_pages(session):
        records.extend(listing_records(soup))

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, session, record['url']): record
            for record in unique.values()
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = future.result() or record['description']
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape agenda detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class OviedofilarmoniaEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oviedofilarmonia_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OviedofilarmoniaEsCrawler().run()


if __name__ == '__main__':
    main()
