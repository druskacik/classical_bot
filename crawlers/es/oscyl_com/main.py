import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.oscyl.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'eventos/')
SOURCE = 'Orquesta Sinfónica de Castilla y León'
HOME_CITY = 'Valladolid'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.6',
}

# Event pages usually append the municipality to touring venues.  These names
# cover the orchestra's recurring Castilla y Leon tour stops and common guest
# appearances; longest names are checked first.
CITY_NAMES = sorted({
    'Alcala de Henares', 'Aranda de Duero', 'Avila', 'Barcelona', 'Benavente',
    'Burgos', 'Carrion de los Condes', 'Ciudad Rodrigo', 'Copenhagen',
    'La Baneza', 'La Granja de San Ildefonso', 'Leon', 'Lerma', 'Madrid',
    'Medina de Rioseco', 'Medina del Campo', 'Miranda de Ebro', 'Palencia',
    'Paris', 'Ponferrada', 'Salamanca', 'San Sebastian', 'Segovia', 'Soria',
    'Toro', 'Tordesillas', 'Valladolid', 'Villafranca del Bierzo', 'Zamora',
}, key=len, reverse=True)

HOME_VENUE_TERMS = (
    'sala sinfonica', 'jesus lopez cobos', 'centro cultural miguel delibes',
    'auditorio miguel delibes', 'teatro calderon',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    import unicodedata

    return ''.join(
        character for character in unicodedata.normalize('NFKD', value)
        if not unicodedata.combining(character)
    ).lower()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def archive_urls(session):
    first = get_soup(session, EVENTS_URL)
    page_numbers = [1]
    for anchor in first.select('a[href*="/eventos/page/"]'):
        match = re.search(r'/eventos/page/(\d+)/?', anchor.get('href', ''))
        if match:
            page_numbers.append(int(match.group(1)))
    last_page = max(page_numbers)

    urls = []
    for page_number in range(1, last_page + 1):
        soup = first if page_number == 1 else get_soup(
            session, urljoin(EVENTS_URL, f'page/{page_number}/')
        )
        for anchor in soup.select('article.eventos h2 a[href]'):
            url = urljoin(SOURCE_URL, anchor.get('href'))
            if '/evento/' in url:
                urls.append(url)
    return list(dict.fromkeys(urls))


def resolve_city(title, venue):
    location = normalized(f'{venue} {title}')
    for city in CITY_NAMES:
        if re.search(rf'(?<!\w){re.escape(normalized(city))}(?!\w)', location):
            return city
    if any(term in normalized(venue) for term in HOME_VENUE_TERMS):
        return HOME_CITY
    return None


def parse_event(soup, url):
    title_node = soup.select_one('article.eventos h1')
    meta = soup.select_one('article.eventos .meta_time_place')
    time_node = meta.select_one('time[datetime]') if meta else None
    if not title_node or not meta or not time_node:
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    raw_datetime = time_node.get('datetime', '')
    match = re.match(r'(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})', raw_datetime)
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    venue_nodes = meta.find_all('span', recursive=False)
    venue = clean_text(venue_nodes[-1].get_text(' ', strip=True)) if venue_nodes else ''
    venue = re.sub(r'^[-\u2013\u2014]\s*', '', venue).strip()
    city = resolve_city(title, venue)
    if not title or not venue or not city:
        return None

    description_node = soup.select_one('article.eventos .entry-content')
    description = clean_text(description_node) or None
    clock = f'{match.group(2)}:{match.group(3)}'
    # Midnight is used as a placeholder throughout the oldest archive.
    if clock == '00:00':
        clock = None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': clock,
        'venue': venue,
        'city': city,
        'country_code': 'ES',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = archive_urls(session)
    records = []

    def fetch(url):
        return parse_event(get_soup(session, url), url)

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch, url): url for url in urls}
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

    return sorted(records, key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['url']
    ))


class OscylComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oscyl_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OscylComCrawler().run()


if __name__ == '__main__':
    main()
