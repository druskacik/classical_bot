import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ort.ro/'
SOURCE = 'Opera Națională Română Timișoara'
CURRENT_URL = urljoin(SOURCE_URL, 'ro/Spectacole.html')
ARCHIVE_URL = urljoin(SOURCE_URL, 'ro/Spectacole-arhiva.html')
DEFAULT_CITY = 'Timișoara'
DEFAULT_VENUE = 'Opera Națională Română Timișoara'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ro-RO,ro;q=0.9,en;q=0.6',
}
MONTHS = {
    'ianuarie': 1, 'februarie': 2, 'martie': 3, 'aprilie': 4,
    'mai': 5, 'iunie': 6, 'iulie': 7, 'august': 8,
    'septembrie': 9, 'octombrie': 10, 'noiembrie': 11, 'decembrie': 12,
}
CITY_VARIANTS = {
    'bucuresti': ('București', 'RO'),
    'bucharest': ('București', 'RO'),
    'cluj napoca': ('Cluj-Napoca', 'RO'),
    'cluj-napoca': ('Cluj-Napoca', 'RO'),
    'timisoara': ('Timișoara', 'RO'),
    'arad': ('Arad', 'RO'),
    'lugoj': ('Lugoj', 'RO'),
    'resita': ('Reșița', 'RO'),
    'oradea': ('Oradea', 'RO'),
    'deva': ('Deva', 'RO'),
    'sibiu': ('Sibiu', 'RO'),
    'brasov': ('Brașov', 'RO'),
    'iasi': ('Iași', 'RO'),
    'craiova': ('Craiova', 'RO'),
    'novi sad': ('Novi Sad', 'RS'),
    'szeged': ('Szeged', 'HU'),
}
VENUE_PATTERNS = (
    r'(Teatrul de vară din Parcul Rozelor)',
    r'(Iulius Gardens)',
    r'(Iulius Town)',
    r'(Sala (?:Mare|Capitol)[^,.\n;]*)',
    r'(Opera Națională București)',
    r'(Opera Națională Română Cluj-Napoca)',
    r'(Teatrul Național[^,.\n;]*)',
    r'(Institutul Francez din Timișoara)',
    r'(Domul Romano-Catolic[^,.\n;]*)',
    r'(Biserica[^,.\n;]*)',
    r'(Catedrala[^,.\n;]*)',
    r'(Filarmonica[^,.\n;]*)',
    r'(Casa de Cultură[^,.\n;]*)',
    r'(Palatul Culturii[^,.\n;]*)',
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fold(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', value.lower())
        if not unicodedata.combining(character)
    )


def parse_date_time(value):
    normalized = fold(value)
    match = re.search(r'\b(\d{1,2})\s+([a-z]+)\s+(20\d{2})\b', normalized)
    if not match or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\bora\s*:\s*([01]?\d|2[0-3]):([0-5]\d)\b', normalized)
    event_time = None
    if time_match:
        event_time = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
    return event_date, event_time


def event_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    return {
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('a[href*="/eveniment/"][href$=".html"]')
        if '/ro/' in link.get('href', '')
    }


def extract_location(banner):
    normalized = fold(banner)
    cities = {
        city_data for variant, city_data in CITY_VARIANTS.items()
        if re.search(rf'\b{re.escape(variant)}\b', normalized)
    }
    other_cities = cities - {(DEFAULT_CITY, 'RO')}
    city, country_code = (
        next(iter(other_cities)) if len(other_cities) == 1 else (DEFAULT_CITY, 'RO')
    )

    venue = None
    for pattern in VENUE_PATTERNS:
        match = re.search(pattern, banner, re.IGNORECASE)
        if match:
            venue = re.sub(r'\s+', ' ', match.group(1)).strip(' ,.-')
            break

    # The institution's calendar is based at its own opera house. Explicitly
    # advertised local outdoor/partner venues override that strong default.
    if city == DEFAULT_CITY:
        return city, venue or DEFAULT_VENUE, country_code
    # A tour city must also have a defensible named venue; otherwise omit it.
    if venue:
        return city, venue, country_code
    return None, None, None


def parse_event(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.titlu-pagina'))
    banner = clean_text(soup.select_one('.data-banner'))
    event_date, event_time = parse_date_time(banner)
    description = clean_text(soup.select_one('.mijloc-pagina > .text'))
    if not title or not event_date:
        return None

    city, venue, country_code = extract_location(banner)
    if not city or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OrtRoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ort_ro',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RO',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = set()
        for listing_url in (CURRENT_URL, ARCHIVE_URL):
            response = session.get(listing_url, timeout=60)
            response.raise_for_status()
            urls.update(event_urls(response.text))

        records = []

        def fetch(url):
            response = session.get(url, timeout=60)
            response.raise_for_status()
            return parse_event(url, response.text)

        with ThreadPoolExecutor(max_workers=12) as executor:
            future_urls = {executor.submit(fetch, url): url for url in urls}
            for future in as_completed(future_urls):
                url = future_urls[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch ORT event detail',
                        event='crawler_fetch_failed',
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
            key=lambda row: (row['date'], row['time_from'] or '', row['url']),
        )


def main():
    OrtRoCrawler().run()


if __name__ == '__main__':
    main()
