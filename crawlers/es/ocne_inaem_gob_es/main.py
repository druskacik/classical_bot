import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ocne.inaem.gob.es/'
HISTORY_URL = f'{SOURCE_URL}programacion/historico/'
SOURCE = 'Orquesta y Coro Nacionales de España'
DEFAULT_CITY = 'Madrid'
DEFAULT_VENUE = 'Auditorio Nacional de Música'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9',
}

MONTHS = {
    'ene': 1, 'feb': 2, 'mar': 3, 'abr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11,
    'dic': 12,
}

# The site normally abbreviates its home venue to the room name. Explicit
# touring venues are mapped only where the venue itself identifies the city.
VENUE_CITIES = {
    'auditorio nacional de música': 'Madrid',
    'auditorio nacional de musica': 'Madrid',
    'fundación juan march': 'Madrid',
    'fundacion juan march': 'Madrid',
    'teatro monumental': 'Madrid',
    'teatro real': 'Madrid',
    'royal albert hall': 'London',
    'barbican centre': 'London',
    'gran teatre del liceu': 'Barcelona',
    'auditorio ciudad de león': 'León',
    'auditorio ciudad de leon': 'León',
    'auditorio de tenerife': 'Santa Cruz de Tenerife',
    'auditorio miguel delibes': 'Valladolid',
    'palacio de carlos v': 'Granada',
    'casa da música': 'Oporto',
    'casa da musica': 'Oporto',
    'auditorio príncipe felipe': 'Oviedo',
    'auditorio principe felipe': 'Oviedo',
    'palacio de la audiencia': 'Soria',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def history_seasons(soup):
    pattern = re.compile(r'/programacion/historico/(\d{2})-(\d{2})/?$')
    seasons = {}
    for link in soup.select('a[href]'):
        url = urljoin(HISTORY_URL, link.get('href'))
        match = pattern.search(urlparse(url).path)
        if match:
            seasons[url.rstrip('/')] = (2000 + int(match.group(1)), 2000 + int(match.group(2)))
    return seasons


def historical_entries(soup, season_years):
    start_year, end_year = season_years
    entries = {}
    for article in soup.select('article.newsItem__wrapper'):
        link = article.select_one('h2.newsItem__title a[href]')
        month_node = article.select_one('.newsItem__date .month')
        if not link or not month_node:
            continue
        month = MONTHS.get(clean_text(month_node).lower().rstrip('.'))
        if not month:
            continue
        url = urljoin(SOURCE_URL, link.get('href'))
        entries[url] = start_year if month >= 7 else end_year
    return entries


def current_entries(soup):
    entries = {}
    for article in soup.select('article.list__item'):
        link = article.select_one('h3 a[href]')
        month_heading = article.find_previous('h2')
        if not link or not month_heading:
            continue
        match = re.search(r'\b(20\d{2})\b', clean_text(month_heading))
        if match:
            entries[urljoin(SOURCE_URL, link.get('href'))] = int(match.group(1))
    return entries


def listing_entries(session):
    home = get_soup(session, SOURCE_URL)
    entries = current_entries(home)
    history = get_soup(session, HISTORY_URL)
    for season_url, years in history_seasons(history).items():
        try:
            entries.update(historical_entries(get_soup(session, season_url), years))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape OCNE historical season',
                event='crawler_page_failed',
                level='warning',
                url=season_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return entries


def resolve_location(soup):
    location = soup.select_one('.item--location .infoBar__text')
    spans = [clean_text(node) for node in location.select('span')] if location else []
    spans = [value for value in spans if value]
    if not spans:
        return None, None

    venue = spans[0]
    if venue.casefold() == 'otros':
        venue = clean_text(' '.join(spans[1:]))
        spans = [venue]
        if not venue:
            return None, None
    normalized = venue.casefold()
    home_room = bool(re.search(r'\bsala\s+(sinf[oó]nica|de c[aá]mara)\b', normalized))
    if home_room:
        return f'{DEFAULT_VENUE} – {venue}', DEFAULT_CITY

    extra = ' '.join(spans[1:])
    city_match = re.search(r'\b\d{5}\b[, ]+([\wÁÉÍÓÚÜÑáéíóúüñ .-]+)', extra)
    if city_match:
        city = city_match.group(1).strip(' ,.-')
        if city:
            return venue, city

    for label, city in VENUE_CITIES.items():
        if label in normalized:
            return venue, city
    return None, None


def description_from_detail(soup):
    parts = []
    synopsis = clean_text(soup.select_one('#sinopsis .obra_summary'))
    if synopsis:
        parts.append(synopsis)

    works = []
    for row in soup.select('#repertorio tbody tr'):
        cells = [clean_text(cell) for cell in row.select('td')]
        work = ': '.join(value for value in cells if value)
        if work:
            works.append(work)
    if works:
        parts.append('Repertorio\n' + '\n'.join(works))
    return '\n\n'.join(parts) or None


def parse_detail(soup, url, year):
    title = clean_text(soup.select_one('h1.documentFirstHeading'))
    venue, city = resolve_location(soup)
    if not title or not venue or not city:
        return []

    description = description_from_detail(soup)
    records = []
    for node in soup.select('.infoBar__text .time'):
        day_text = clean_text(node.select_one('.day'))
        month_text = clean_text(node.select_one('.month')).lower().rstrip('.')
        hour_text = clean_text(node.select_one('.hour')).upper().rstrip('H').strip()
        try:
            event_date = date(year, MONTHS[month_text], int(day_text)).isoformat()
        except (KeyError, TypeError, ValueError):
            continue
        time_match = re.fullmatch(r'(\d{1,2}):(\d{2})', hour_text)
        time_from = None
        if time_match and int(time_match.group(1)) < 24 and int(time_match.group(2)) < 60:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    entries = listing_entries(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_soup, session, url): (url, year)
            for url, year in entries.items()
        }
        for future in as_completed(futures):
            url, year = futures[future]
            try:
                records.extend(parse_detail(future.result(), url, year))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape OCNE concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class OcneInaemGobEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ocne_inaem_gob_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OcneInaemGobEsCrawler().run()


if __name__ == '__main__':
    main()
