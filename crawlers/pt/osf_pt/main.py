import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://osf.pt/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda/')
SOURCE = 'Orquestra Sem Fronteiras'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}
MONTHS = {
    'janeiro': 1, 'fevereiro': 2, 'março': 3, 'abril': 4,
    'maio': 5, 'junho': 6, 'julho': 7, 'agosto': 8,
    'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12,
}

# The site supplies a free-text venue rather than structured geography. These
# stable venue/place names cover the touring archive; explicit city text is
# preferred by city_for_event() before these venue defaults are consulted.
PLACE_CITIES = (
    ('valencia del mombuey', 'Valencia del Mombuey', 'ES'),
    ('villanueva del fresno', 'Villanueva del Fresno', 'ES'),
    ('medina de las torres', 'Medina de las Torres', 'ES'),
    ('são joão da madeira', 'São João da Madeira', 'PT'),
    ('figueira de castelo rodrigo', 'Figueira de Castelo Rodrigo', 'PT'),
    ('vila nova de foz côa', 'Vila Nova de Foz Côa', 'PT'),
    ('vila nova da barquinha', 'Vila Nova da Barquinha', 'PT'),
    ('santa maria da feira', 'Santa Maria da Feira', 'PT'),
    ('oliveira do hospital', 'Oliveira do Hospital', 'PT'),
    ('santa cruz da trapa', 'Santa Cruz da Trapa', 'PT'),
    ('são joão da madeira', 'São João da Madeira', 'PT'),
    ('póvoa de varzim', 'Póvoa de Varzim', 'PT'),
    ('marinha grande', 'Marinha Grande', 'PT'),
    ('pedrógão grande', 'Pedrógão Grande', 'PT'),
    ('pedrogao grande', 'Pedrógão Grande', 'PT'),
    ('peso da régua', 'Peso da Régua', 'PT'),
    ('castelo branco', 'Castelo Branco', 'PT'),
    ('castelo de vide', 'Castelo de Vide', 'PT'),
    ('campo maior', 'Campo Maior', 'PT'),
    ('celorico de basto', 'Celorico de Basto', 'PT'),
    ('idanha-a-nova', 'Idanha-a-Nova', 'PT'),
    ('idanha-a-velha', 'Idanha-a-Velha', 'PT'),
    ('centro cultural raiano', 'Idanha-a-Nova', 'PT'),
    ('toulões', 'Toulões', 'PT'), ('penha garcia', 'Penha Garcia', 'PT'),
    ('monsanto', 'Monsanto', 'PT'), ('pavia', 'Pavia', 'PT'),
    ('termas de monfortinho', 'Monfortinho', 'PT'),
    ('monfortinho', 'Monfortinho', 'PT'),
    ('montemor-o-novo', 'Montemor-o-Novo', 'PT'),
    ('vila do conde', 'Vila do Conde', 'PT'),
    ('rio de janeiro', 'Rio de Janeiro', 'BR'),
    ('real gabinete português de leitura', 'Rio de Janeiro', 'BR'),
    ('badajoz', 'Badajoz', 'ES'), ('olivenza', 'Olivenza', 'ES'),
    ('mérida', 'Mérida', 'ES'), ('cáceres', 'Cáceres', 'ES'),
    ('almendralejo', 'Almendralejo', 'ES'), ('alconchel', 'Alconchel', 'ES'),
    ('cheles', 'Cheles', 'ES'), ('zafra', 'Zafra', 'ES'),
    ('castelo de leiria', 'Leiria', 'PT'), ('leiria', 'Leiria', 'PT'),
    ('aveirense', 'Aveiro', 'PT'), ('aveiro', 'Aveiro', 'PT'),
    ('arganil', 'Arganil', 'PT'), ('arraiolos', 'Arraiolos', 'PT'),
    ('águeda', 'Águeda', 'PT'), ('agueda', 'Águeda', 'PT'),
    ('alcobaça', 'Alcobaça', 'PT'), ('alcanena', 'Alcanena', 'PT'),
    ('amarante', 'Amarante', 'PT'), ('barcelos', 'Barcelos', 'PT'),
    ('belmonte', 'Belmonte', 'PT'), ('coimbra', 'Coimbra', 'PT'),
    ('covilhã', 'Covilhã', 'PT'), ('elvas', 'Elvas', 'PT'),
    ('faro', 'Faro', 'PT'), ('figuras', 'Faro', 'PT'),
    ('salgados palace', 'Albufeira', 'PT'),
    ('fundão', 'Fundão', 'PT'), ('gouveia', 'Gouveia', 'PT'),
    ('guarda', 'Guarda', 'PT'), ('lagoa', 'Lagoa', 'PT'),
    ('lisboa', 'Lisboa', 'PT'), ('gulbenkian', 'Lisboa', 'PT'),
    ('campo pequeno', 'Lisboa', 'PT'), ('são luiz', 'Lisboa', 'PT'),
    ('são roque', 'Lisboa', 'PT'), ('são bento', 'Lisboa', 'PT'),
    ('lu.ca', 'Lisboa', 'PT'), ('luís de camões', 'Lisboa', 'PT'),
    ('loulé', 'Loulé', 'PT'), ('louletano', 'Loulé', 'PT'),
    ('lousã', 'Lousã', 'PT'), ('maia', 'Maia', 'PT'),
    ('marvão', 'Marvão', 'PT'), ('mealhada', 'Mealhada', 'PT'),
    ('mértola', 'Mértola', 'PT'), ('mora', 'Mora', 'PT'),
    ('matosinhos', 'Matosinhos', 'PT'), ('nelas', 'Nelas', 'PT'),
    ('óbidos', 'Óbidos', 'PT'), ('ourém', 'Ourém', 'PT'),
    ('penafiel', 'Penafiel', 'PT'), ('penamacor', 'Penamacor', 'PT'),
    ('pinhel', 'Pinhel', 'PT'), ('portimão', 'Portimão', 'PT'),
    ('porto', 'Porto', 'PT'), ('sagres', 'Albufeira', 'PT'),
    ('santarém', 'Santarém', 'PT'), ('sabugal', 'Sabugal', 'PT'),
    ('salvaterra do extremo', 'Salvaterra do Extremo', 'PT'),
    ('seixal', 'Seixal', 'PT'), ('sertã', 'Sertã', 'PT'),
    ('sintra', 'Sintra', 'PT'), ('olga cadaval', 'Sintra', 'PT'),
    ('torres novas', 'Torres Novas', 'PT'), ('viseu', 'Viseu', 'PT'),
    ('vila real', 'Vila Real', 'PT'), ('bendada', 'Bendada', 'PT'),
)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([a-zç]+),?\s+(\d{3,4})', clean_text(value).lower())
    if not match or match.group(2) not in MONTHS:
        return None
    year = int(match.group(3))
    if year < 1900:
        return None
    try:
        return date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def event_id(url):
    values = parse_qs(urlparse(url).query).get('evento_id', [])
    return values[0] if values and values[0].isdigit() else None


def parse_listing_page(page_html, page_url):
    soup = BeautifulSoup(page_html, 'html.parser')
    events = []
    for card in soup.select('.osf-card'):
        link = card.select_one('a[href*="evento_id="]')
        title = clean_text(card.select_one('.osf-truncated-title'))
        event_date = parse_date(clean_text(card.select_one('.event-date')))
        if not link or not title or not event_date:
            continue
        url = urljoin(page_url, link.get('href', ''))
        identifier = event_id(url)
        if not identifier:
            continue
        events.append({
            'id': identifier,
            'title': title,
            'date': event_date,
            'url': url,
            'listing_location': clean_text(card.select_one('.osf-truncated-subtitle')),
        })
    pages = []
    for link in soup.select('a.page-numbers[href]'):
        match = re.search(r'/agenda/page/(\d+)/', link.get('href', ''))
        if match:
            pages.append(int(match.group(1)))
    return events, max(pages, default=1)


def city_for_event(title, location):
    searchable = f'{location} | {title}'.casefold()
    for marker, city, country_code in PLACE_CITIES:
        if marker.casefold() in searchable:
            return city, country_code
    return None


def split_venue(location, city):
    venue = clean_text(location).strip(' ,|.-')
    if not venue:
        return None
    parts = [part.strip(' ,|.-') for part in re.split(r'\s*[|,]\s*', venue) if part.strip(' ,|.-')]
    if len(parts) > 1 and parts[-1].casefold() == city.casefold():
        venue = ' | '.join(parts[:-1])
    venue = re.sub(r',?\s+em\s+' + re.escape(city) + r'\.?$', '', venue, flags=re.IGNORECASE)
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue


def parse_detail(page_html, listing):
    soup = BeautifulSoup(page_html, 'html.parser')
    main = soup.select_one('main#primary')
    if not main:
        return None
    headings = main.select('.entry-content h5')
    detail_location = clean_text(headings[1]) if len(headings) > 1 else ''
    location = detail_location or listing['listing_location']
    geography = city_for_event(listing['title'], location)
    if not geography:
        return None
    city, country_code = geography
    venue = split_venue(location, city)
    if not venue:
        return None

    time_from = None
    if headings:
        time_match = re.search(r'\b([01]?\d|2[0-3])[h:]([0-5]\d)\b', clean_text(headings[0]))
        if time_match:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_parts = []
    for selector in ('.event-synopsys', '.event-program', '.event-artists'):
        for node in main.select(selector):
            value = clean_text(node)
            if value and value not in description_parts:
                description_parts.append(value)

    return {
        'title': listing['title'],
        'date': listing['date'],
        'url': listing['url'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
    }


class OsfPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='osf_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def _get(self, session, url):
        last_error = None
        for attempt in range(4):
            try:
                response = session.get(url, timeout=45)
                if response.status_code not in {403, 429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return response
                last_error = requests.HTTPError(f'HTTP {response.status_code}', response=response)
            except requests.RequestException as error:
                last_error = error
            if attempt < 3:
                time.sleep(2 ** attempt)
        raise last_error

    def _detail(self, listing):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = self._get(session, listing['url'])
            return parse_detail(response.text, listing)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch OSF event detail', event='crawler_detail_fetch_failed',
                level='warning', url=listing['url'], error_type=type(error).__name__,
                error_message=str(error),
            )
            return None

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            first_response = self._get(session, AGENDA_URL)
            listings, last_page = parse_listing_page(first_response.text, AGENDA_URL)
            for page in range(2, last_page + 1):
                page_url = urljoin(AGENDA_URL, f'page/{page}/?default_tab=1')
                response = self._get(session, page_url)
                page_listings, _ = parse_listing_page(response.text, page_url)
                listings.extend(page_listings)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch OSF agenda', event='crawler_fetch_failed', level='error',
                url=AGENDA_URL, error_type=type(error).__name__, error_message=str(error),
            )
            raise

        unique = {}
        for listing in listings:
            unique.setdefault(listing['id'], listing)
        records = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(self._detail, listing) for listing in unique.values()]
            for future in as_completed(futures):
                record = future.result()
                if record:
                    records.append(record)
        if not records:
            raise ValueError('No OSF events had a parseable date, venue, and city')
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    OsfPtCrawler().run()


if __name__ == '__main__':
    main()
