import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nuovaconsonanza.it/'
EVENTS_URL = urljoin(SOURCE_URL, 'eventi.html')
ARCHIVE_URL = urljoin(SOURCE_URL, 'index.php?pagina=gestione_archivio')
SOURCE = 'Nuova Consonanza'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def archive_years(session):
    soup = get_soup(session, ARCHIVE_URL)
    return [
        option['value'] for option in soup.select('#scelta_anno option[value]')
        if re.fullmatch(r'\d{4}', option.get('value', ''))
    ]


def archive_selections(session, year):
    soup = get_soup(
        session,
        urljoin(SOURCE_URL, 'pagine/archivio.php'),
        {'scelta_anno': year, 'id_lingua_cerca_archivio': '10'},
    )
    selections = []
    for option in soup.select('option[value]'):
        match = re.fullmatch(r'([a-zA-Z_]+)-(\d+)', option.get('value', ''))
        if match:
            selections.append((year, match.group(1), match.group(2)))
    return selections


def parse_occurrences(soup):
    occurrences = []
    for date_node in soup.select('.box_festival_data'):
        container = date_node.find_parent(class_='box_eventi_cad') or date_node.find_parent('table')
        if container is None:
            continue
        link = container.select_one('h1 a[href], h2 a[href], p a[href]')
        venue_node = container.select_one('.box_festival_pin')
        time_node = container.select_one('.box_festival_ora')
        category_node = container.select_one('.box_eventi_categoria')
        if category_node is None:
            category_node = container.find(
                lambda tag: tag.name == 'div' and clean_text(tag).casefold() in {
                    'concerto', 'festival', 'spettacolo', 'performance',
                }
            )
        if link is None or venue_node is None:
            continue
        try:
            event_date = datetime.strptime(clean_text(date_node), '%d/%m/%Y').date().isoformat()
        except ValueError:
            continue
        category = clean_text(category_node).casefold() if category_node else ''
        # Archive festival responses identify the collection rather than repeating
        # a type on each concrete occurrence.
        archive_heading = soup.select_one('div strong')
        if not category and clean_text(archive_heading).casefold() == 'festival':
            category = 'festival'
        if category not in {'concerto', 'festival', 'spettacolo', 'performance'}:
            continue
        time_text = clean_text(time_node)
        time_from = time_text if re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_text) else None
        if time_from == '00:00':
            time_from = None
        occurrences.append({
            'date': event_date,
            'url': urljoin(SOURCE_URL, link.get('href', '')),
            'venue': clean_text(venue_node),
            'time_from': time_from,
        })
    return occurrences


def infer_city(venue, detail_text):
    combined = f'{venue}\n{detail_text}'
    city_patterns = {
        'Roma': r'\b(?:Roma|Rome)\b',
        'Milano': r'\bMilano\b',
        'L’Aquila': r"\bL['’ ]Aquila\b",
        'Firenze': r'\bFirenze\b',
        'Napoli': r'\bNapoli\b',
        'Palermo': r'\bPalermo\b',
        'Bologna': r'\bBologna\b',
        'Torino': r'\bTorino\b',
        'Venezia': r'\bVenezia\b',
        'Perugia': r'\bPerugia\b',
        'Rieti': r'\bRieti\b',
        'Latina': r'\bLatina\b',
        'Viterbo': r'\bViterbo\b',
    }
    for city, pattern in city_patterns.items():
        if re.search(pattern, combined, re.I):
            return city
    # Nuova Consonanza is a Rome presenter and its own event/archive calendar is
    # Rome-based. Explicitly named touring cities above take precedence.
    return 'Roma'


def parse_detail(session, occurrence):
    soup = get_soup(session, occurrence['url'])
    title_node = soup.select_one('h1')
    description_node = soup.select_one('#testo_pagina_interna_generale')
    title = clean_text(title_node).split('\n', 1)[0]
    if not title:
        title_meta = soup.select_one('meta[property="og:title"]')
        title = clean_text(title_meta.get('content')) if title_meta else ''
    if not title:
        # The site's PHP cache occasionally returns an empty page body with 200.
        soup = get_soup(session, occurrence['url'], {'crawler_retry': '1'})
        title_node = soup.select_one('h1')
        description_node = soup.select_one('#testo_pagina_interna_generale')
        title = clean_text(title_node).split('\n', 1)[0]
    venue = occurrence['venue']
    if not title or not venue:
        return None
    description = clean_text(description_node) or None
    city = infer_city(venue, clean_text(description_node))
    time_from = occurrence['time_from']
    if time_from is None:
        time_match = re.search(r'\bore\s+([01]?\d|2[0-3])[.:]([0-5]\d)', clean_text(soup), re.I)
        if time_match:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
    return {
        'title': title,
        'date': occurrence['date'],
        'url': occurrence['url'],
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class NuovaConsonanzaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nuovaconsonanza_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
            current = parse_occurrences(get_soup(session, EVENTS_URL))
            selections = []
            for year in archive_years(session):
                selections.extend(archive_selections(session, year))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Nuova Consonanza indexes',
                event='crawler_fetch_failed', level='error', url=SOURCE_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        def fetch_archive(selection):
            year, kind, item_id = selection
            thread_session = requests.Session()
            thread_session.headers.update(HEADERS)
            soup = get_soup(
                thread_session,
                urljoin(SOURCE_URL, 'pagine/archivio_seleziona.php'),
                {
                    'scelta_anno': year, 'scelta_archivio': item_id,
                    'pagina_archivio': kind, 'id_lingua_cerca_archivio': '10',
                },
            )
            return parse_occurrences(soup)

        occurrences = list(current)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_archive, item): item for item in selections}
            for future in as_completed(futures):
                try:
                    occurrences.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Nuova Consonanza archive item',
                        event='crawler_item_failed', level='warning',
                        url=ARCHIVE_URL, error_type=type(error).__name__,
                        error_message=str(error),
                    )

        unique = {(row['url'], row['date'], row['time_from'], row['venue']): row for row in occurrences}
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = []
            for occurrence in unique.values():
                detail_session = requests.Session()
                detail_session.headers.update(HEADERS)
                futures.append(executor.submit(parse_detail, detail_session, occurrence))
            for future in as_completed(futures):
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Nuova Consonanza event detail',
                        event='crawler_item_failed', level='warning',
                        url=SOURCE_URL, error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    NuovaConsonanzaItCrawler().run()


if __name__ == '__main__':
    main()
