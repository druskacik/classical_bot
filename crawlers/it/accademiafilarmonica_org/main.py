import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.accademiafilarmonica.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'eventi')
SOURCE = 'Accademia Filarmonica di Verona'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'GEN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAG': 5, 'GIU': 6,
    'LUG': 7, 'AGO': 8, 'SET': 9, 'OTT': 10, 'NOV': 11, 'DIC': 12,
}

# Longest and most specific names come first. These are the venues used by the
# archive; matching them explicitly avoids treating dates or programme prose as
# locations.
VENUES = [
    ('Sala Convegni del Palazzo della Gran Guardia', 'Sala Convegni del Palazzo della Gran Guardia'),
    ('Auditorium Nuovo Montemezzi del Conservatorio', 'Auditorium Nuovo Montemezzi del Conservatorio'),
    ('Sala Maffeiana del Teatro Filarmonico di Verona', 'Sala Maffeiana del Teatro Filarmonico'),
    ('Sala Maffeiana del Teatro Filarmonico', 'Sala Maffeiana del Teatro Filarmonico'),
    ('Chiesa di S. Tomaso Cantuariense', 'Chiesa di San Tomaso Cantuariense'),
    ('Chiesa di San Nicolò all’Arena', 'Chiesa di San Nicolò all’Arena'),
    ('Polo Santa Marta – Gradinata Ovest', 'Polo Santa Marta – Gradinata Ovest'),
    ('Polo Santa Marta Gradinata Ovest', 'Polo Santa Marta – Gradinata Ovest'),
    ('Polo Santa Marta – Corte Ovest', 'Polo Santa Marta – Corte Ovest'),
    ('Polo Santa Marta - Corte Ovest', 'Polo Santa Marta – Corte Ovest'),
    ('Polo Santa Marta di Verona – Corte Ovest', 'Polo Santa Marta – Corte Ovest'),
    ('Polo Santa Marta – Aula SMT06', 'Polo Santa Marta – Aula SMT06'),
    ('Polo Santa Marta - Aula SMT06', 'Polo Santa Marta – Aula SMT06'),
    ('Polo Santa Marta open air', 'Polo Santa Marta – Open Air'),
    ('Bastione delle Maddalene', 'Bastione delle Maddalene'),
    ('Teatro Filarmonico di Verona', 'Teatro Filarmonico di Verona'),
    ('Teatro Filarmonico', 'Teatro Filarmonico di Verona'),
    ('Basilica di San Zeno', 'Basilica di San Zeno'),
    ('Aula Magna del Polo Zanotto', 'Aula Magna del Polo Zanotto'),
    ('Sala Maffeiana', 'Sala Maffeiana del Teatro Filarmonico'),
]


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def detail_urls(soup):
    urls = []
    seen = set()
    for link in soup.select('a[href^="/eventi/"][href]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_date(node):
    match = re.fullmatch(r'\s*(\d{1,2})\s+([A-Z]{3})\s+(\d{4})\s*', clean_text(node))
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except (KeyError, ValueError):
        return None


def find_venue(*values):
    for value in values:
        folded = clean_text(value).casefold()
        for name, canonical in VENUES:
            if name.casefold() in folded:
                return canonical
    return None


def parse_time(value):
    match = re.search(r'\bore\s*([01]?\d|2[0-3])(?:[.,:]([0-5]\d))?\b', value, re.I)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def parse_detail(soup, url):
    page_title_node = soup.select_one('section h1.blu') or soup.select_one('section h1')
    header_node = soup.select_one('section h2.nero')
    page_title = clean_text(page_title_node)
    header = clean_text(header_node)
    if not page_title:
        return []

    records = []
    for event in soup.select('.entry.event'):
        event_date = parse_date(event.select_one('.datona'))
        detail_node = event.select_one('td:not(.datona)')
        if not event_date or detail_node is None:
            # The site also styles undated festival descriptions as events.
            continue

        description = clean_text(detail_node)
        title_node = detail_node.select_one('h3')
        title = clean_text(title_node) or page_title
        venue = find_venue(description.split('\n', 1)[0], header)
        if not title or not venue:
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(description.split('\n', 1)[0]),
            'venue': venue,
            'city': 'Verona',
            'description': description or None,
        })
    return records


class AccademiafilarmonicaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='accademiafilarmonica_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            urls = detail_urls(get_soup(session, EVENTS_URL))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Accademia Filarmonica di Verona event listing',
                event='crawler_fetch_failed', level='error', url=EVENTS_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        for url in urls:
            try:
                records.extend(parse_detail(get_soup(session, url), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Accademia Filarmonica di Verona event',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    AccademiafilarmonicaOrgCrawler().run()


if __name__ == '__main__':
    main()
