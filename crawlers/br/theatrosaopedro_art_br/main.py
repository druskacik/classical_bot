import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://theatrosaopedro.art.br/'
PROGRAM_URL = f'{SOURCE_URL}programacao/'
SOURCE = 'Theatro São Pedro'
CITY = 'São Paulo'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def canonical_event_url(url):
    parsed = urlparse(url)
    event_id = (parse_qs(parsed.query).get('id') or [''])[0]
    if not event_id.isdigit():
        return ''
    return urlunparse(('https', 'theatrosaopedro.art.br', '/evento', '', urlencode({'id': event_id}), ''))


def listing_urls(session):
    soup = get_soup(session, PROGRAM_URL)
    urls = {
        canonical_event_url(anchor.get('href', ''))
        for anchor in soup.select('.eventos .evento a[href*="evento?id="]')
    }
    return sorted(url for url in urls if url)


def detail_fields(soup):
    fields = {}
    for label in soup.select('.info-evento'):
        key = clean_text(label).rstrip(':').casefold()
        parent_text = clean_text(label.parent)
        value = parent_text[len(clean_text(label)):].strip(' :\n')
        fields[key] = value
    return fields


def parse_date(value):
    match = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', value or '')
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%d/%m/%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*(?::|h)\s*([0-5]\d)?\b', value or '', re.I)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2) or "00"}'


def parse_event(session, url):
    soup = get_soup(session, url)
    title = clean_text(soup.select_one('.titulo-evento'))
    fields = detail_fields(soup)
    event_date = parse_date(fields.get('data'))
    venue = clean_text(fields.get('local'))
    description_node = soup.select_one('.conteudo-texto-evento > div')
    description = clean_text(description_node) or None

    if not title or not event_date or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(fields.get('horário') or fields.get('horario')),
        'venue': venue,
        'city': CITY,
        'country_code': 'BR',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = make_session()
    urls = listing_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_event, session, url): url for url in urls}
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

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class TheatroSaoPedroArtBrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theatrosaopedro_art_br',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BR',
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
    TheatroSaoPedroArtBrCrawler().run()


if __name__ == '__main__':
    main()
