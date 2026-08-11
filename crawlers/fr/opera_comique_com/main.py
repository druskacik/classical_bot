import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera-comique.com/fr'
PROGRAMME_URL = f'{SOURCE_URL}/spectacles'
ARCHIVE_URL = f'{PROGRAMME_URL}/archives'
SOURCE = 'Opéra-Comique'
HOME_VENUE = "Théâtre National de l'Opéra-Comique"
HOME_CITY = 'Paris'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.5',
}

# The 2026-27 regional tour prints only the host venue on each performance
# card.  The same first-party page identifies the tour cities; this mapping
# prevents those performances from incorrectly inheriting the Paris theatre.
TOUR_VENUES = {
    'tap - scene nationale de grand poitiers': 'Poitiers',
    'les 3t - scene conventionnee de chatellerault': 'Châtellerault',
    "scene nationale d'albi-tarn": 'Albi',
    'theatre olympia': 'Arcachon',
    'scene nationale du sud-aquitain': 'Bayonne',
    'le pole': 'Mont-de-Marsan',
    'theatre du jour': 'Agen',
    'theatre des quatre saisons': 'Gradignan',
    "l'odyssee": 'Périgueux',
    'le trident': 'Cherbourg',
    'theatre municipal de fontainebleau': 'Fontainebleau',
    'les 2 scenes': 'Besançon',
    'theatre alexandre-dumas': 'Saint-Germain-en-Laye',
    'theatre imperial de compiegne': 'Compiègne',
    'espace jean legendre': 'Compiègne',
    'opera de rennes': 'Rennes',
    'la passerelle': 'Saint-Brieuc',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def folded(value):
    import unicodedata

    normalized = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(char for char in normalized if not unicodedata.combining(char))


def canonical_url(value):
    url = urljoin(SOURCE_URL, value or '')
    parts = urlsplit(url)
    path = re.sub(r'^/index(?:%2[eE]|\.)php', '', parts.path)
    return urlunsplit(('https', 'www.opera-comique.com', path.rstrip('/'), '', ''))


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    return session


def page_event_urls(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return {
        canonical_url(anchor.get('href'))
        for anchor in soup.select('main a[href]')
        if re.search(r'/(?:index(?:%2[eE]|\.)php/)?fr/spectacles/[^/#?]+$', anchor.get('href', ''))
        and not anchor.get('href', '').rstrip('/').endswith('/archives')
    }


def catalogue_urls(session):
    urls = page_event_urls(session, PROGRAMME_URL)
    seen_archive_pages = set()
    for page in range(100):
        page_urls = page_event_urls(session, ARCHIVE_URL, {'page': page})
        fingerprint = frozenset(page_urls)
        if not page_urls or fingerprint in seen_archive_pages:
            break
        seen_archive_pages.add(fingerprint)
        urls.update(page_urls)
    else:
        raise RuntimeError('Archive pagination exceeded 100 pages')
    return urls


def iter_event_json(soup):
    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            if value.get('@type') in {'Event', 'TheaterEvent', 'MusicEvent'}:
                yield value
            if value.get('@graph'):
                yield from walk(value['@graph'])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        yield from walk(value)


def parsed_start_dates(soup):
    dates = []
    for event in iter_event_json(soup):
        value = event.get('startDate')
        if not isinstance(value, str):
            continue
        try:
            dates.append(datetime.fromisoformat(value.replace('Z', '+00:00')).date())
        except ValueError:
            continue
    return dates


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*h\s*([0-5]\d)?\b', clean_text(value), re.I)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def parse_place(value):
    text = clean_text(value)
    text = re.sub(r'^\[Spectacle hors les murs\]\s*', '', text, flags=re.I).strip(' -')
    if not text or folded(text) in {'seance scolaire', 'complet', 'places disponibles'}:
        return HOME_VENUE, HOME_CITY

    postal = re.search(r'\b\d{5}\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .\'’-]+)', text)
    city = clean_text(postal.group(1)).strip(' .,-') if postal else None
    venue = re.split(r'\s+-\s+(?=\d)|,\s*(?=\d)', text, maxsplit=1)[0].strip(' ,.-')
    if postal and not city:
        return None, None
    if postal and city:
        return venue or None, city

    normalized = folded(venue).replace('–', '-').replace('—', '-')
    for known_venue, known_city in TOUR_VENUES.items():
        if known_venue in normalized:
            return venue, known_city

    named_city = re.fullmatch(r'(.+),\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\'’ -]+)', text)
    if named_city:
        return named_city.group(1).strip(' ,.-'), named_city.group(2).strip()
    # Explicit non-address text is often a touring venue.  Without a city it
    # is unsafe to substitute Paris, so skip it.
    return None, None


def description_from(soup):
    parts = []
    for selector in ('#presentation', '.show-section--presentation', '#distribution'):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    if not parts:
        for event in iter_event_json(soup):
            description = clean_text(event.get('description'))
            if description:
                parts.append(description)
                break
    return '\n\n'.join(parts) or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    if not title:
        return []
    description = description_from(soup)
    structured_dates = parsed_start_dates(soup)
    cards = soup.select('.session-card')
    records = []
    for index, card in enumerate(cards):
        if index >= len(structured_dates):
            continue
        time_from = parse_time(card.select_one('.session-time'))
        place_node = card.select_one('.session-text')
        venue, city = parse_place(place_node)
        if not venue or not city:
            continue
        records.append({
            'title': title,
            'date': structured_dates[index].isoformat(),
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class OperaComiqueComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_comique_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        urls = catalogue_urls(session)
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(session.get, url, timeout=60): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    records.extend(parse_detail(response.text, url))
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Opéra-Comique event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda row: (
            row['date'], row['time_from'] or '', row['title'], row['venue'],
        ))


def main():
    OperaComiqueComCrawler().run()


if __name__ == '__main__':
    main()
