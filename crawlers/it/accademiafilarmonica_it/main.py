import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://accademiafilarmonica.it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'concerti')
ARCHIVE_URL = urljoin(SOURCE_URL, 'concerti/archivio-stagioni')
SOURCE = 'Accademia Filarmonica di Bologna'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


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


def listing_urls(session, base_url, parameters=None):
    """Read every server-rendered page until a page contributes no new events."""
    urls = []
    seen = set()
    for page in range(100):
        query = dict(parameters or {})
        query['page'] = page
        response = session.get(base_url, params=query, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        page_urls = []
        for link in soup.select('h2.titolo-concerto a[href]'):
            url = urljoin(SOURCE_URL, link.get('href', ''))
            if url.startswith(f'{SOURCE_URL}concerti/') and url not in seen:
                seen.add(url)
                urls.append(url)
                page_urls.append(url)
        if not page_urls:
            break
    return urls


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b', value)
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1))
        ).isoformat()
    except (KeyError, ValueError):
        return None


def parse_detail(soup, url):
    article = soup.select_one('article.node--type-concerto')
    title_node = soup.select_one('h1.title')
    date_node = article.select_one('.metadati .data') if article else None
    place_node = article.select_one('.field--name-field-luogo-evento') if article else None
    if not article or not title_node or not date_node or not place_node:
        return None

    title = clean_text(title_node)
    event_date = parse_date(clean_text(date_node))
    place = clean_text(place_node)
    # The text after the first comma is a street address, not part of the venue.
    venue = place.split(',', 1)[0].strip()
    if not title or not event_date or not venue:
        return None

    time_node = article.select_one('.metadati .orario')
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(time_node))
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None

    description_nodes = article.select(
        '.field--name-body, .field--name-field-conduttori, '
        '.field--name-field-programma, .field--name-field-titolo, '
        '.field--name-field-testo'
    )
    description = clean_text('\n\n'.join(clean_text(node) for node in description_nodes)) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Bologna',
        'description': description,
    }


class AccademiaFilarmonicaItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='accademiafilarmonica_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            urls = listing_urls(session, CALENDAR_URL, {'tid': 'All'})
            for url in listing_urls(session, ARCHIVE_URL):
                if url not in urls:
                    urls.append(url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Accademia Filarmonica concert listings',
                event='crawler_fetch_failed', level='error', url=CALENDAR_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        for url in urls:
            try:
                record = parse_detail(get_soup(session, url), url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Accademia Filarmonica concert',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    AccademiaFilarmonicaItCrawler().run()


if __name__ == '__main__':
    main()
