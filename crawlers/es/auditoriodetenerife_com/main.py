import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.auditoriodetenerife.com/es/'
SOURCE = 'Auditorio de Tenerife'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}

# These are all categories exposed by the site's client-side filters. The server
# returns the complete archive year regardless of the category query. Keep the
# full concrete event-card feed because Música includes jazz/pop while adjacent
# categories can contain eligible dance, music theatre, family, or crossover
# performances; the potential-event classifier makes the final scope decision.
INCLUDED_CATEGORIES = {
    'Artes Escénicas',
    'Congresos',
    'Danza',
    'FAM',
    'Familias',
    'Música',
    'Ópera',
    'Otros',
    'Teatro',
    'Teatro Musical',
}

MONTHS = {
    'ene': 1,
    'feb': 2,
    'mar': 3,
    'abr': 4,
    'may': 5,
    'jun': 6,
    'jul': 7,
    'ago': 8,
    'sept': 9,
    'sep': 9,
    'oct': 10,
    'nov': 11,
    'dic': 12,
}

VENUE_CITIES = {
    'auditorio de tenerife': 'Santa Cruz de Tenerife',
    'espacio la granja': 'Santa Cruz de Tenerife',
    'teatro guimerá': 'Santa Cruz de Tenerife',
    'teatro guimera': 'Santa Cruz de Tenerife',
    'teatro leal': 'San Cristóbal de La Laguna',
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


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_urls(session):
    # The public Histórico selector exposes every retained year from 2017.
    # Also request the default upcoming view so events beyond the current year
    # are not lost.
    pages = [(SOURCE_URL, None)]
    pages.extend((SOURCE_URL, {'start_date': str(year)}) for year in range(2017, date.today().year + 1))
    urls = set()
    for url, params in pages:
        soup = get_soup(session, url, params=params)
        for item in soup.select('.adt-item'):
            category = clean_text(item.get('data-category'))
            link = item.select_one('.adt-item-title a[href]')
            if category in INCLUDED_CATEGORIES and link:
                urls.add(link['href'].split('#', 1)[0])
    return sorted(urls)


def resolve_venue(soup):
    blocks = soup.select('.adt-item-show-tab-contents .adt-item-basic-content')
    location_block = next((block for block in blocks if block.select_one('.cattitle')), None)
    if not location_block:
        return None, None
    block = BeautifulSoup(str(location_block), 'html.parser')
    heading = block.select_one('.adt-item-basic-content-title')
    if heading:
        heading.decompose()
    venue = clean_text(block)
    if not venue:
        return None, None
    normalized = venue.casefold()
    for venue_name, city in VENUE_CITIES.items():
        if venue_name in normalized:
            return venue, city
    return None, None


def parse_dates(soup):
    dates = []
    for node in soup.select('.adt-item-show-dates > .adt-item-dates-date'):
        text = clean_text(node).casefold()
        match = re.search(r'(\d{1,2})\s+([a-záéíóú]+)\s+(\d{2,4})$', text)
        if not match:
            continue
        month = MONTHS.get(match.group(2))
        year = int(match.group(3))
        if year < 100:
            year += 2000
        if not month:
            continue
        try:
            dates.append(date(year, month, int(match.group(1))).isoformat())
        except ValueError:
            continue
    return dates


def parse_times(soup):
    times = []
    for row in soup.select('.adt-item-show-tickets-container .adt-item-performances'):
        match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(row))
        times.append(f'{int(match.group(1)):02d}:{match.group(2)}' if match else None)
    return times


def description_text(soup):
    parts = []
    subtitle = clean_text(soup.select_one('.adt-item-show-description'))
    if subtitle:
        parts.append(subtitle)
    excluded = {'entradas', 'abonos', 'galería', 'galeria', 'multimedia'}
    for node in soup.select('.adt-item-show-tab-contents .adt-item-tab-content'):
        text = clean_text(node)
        heading = text.split('\n', 1)[0].casefold() if text else ''
        if text and heading not in excluded and text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def parse_detail(url, soup):
    title = clean_text(soup.select_one('h1.adt-item-show-title'))
    venue, city = resolve_venue(soup)
    dates = parse_dates(soup)
    if not title or not venue or not city or not dates:
        return []
    times = parse_times(soup)
    if len(times) != len(dates):
        times = [None] * len(dates)
    description = description_text(soup)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': times[index],
            'venue': venue,
            'city': city,
            'country_code': 'ES',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for index, event_date in enumerate(dates)
    ]


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_detail(url, future.result()))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']))


class AuditorioDeTenerifeComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='auditoriodetenerife_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    AuditorioDeTenerifeComCrawler().run()


if __name__ == '__main__':
    main()
