import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fundacioncanal.com/ciclo-musica-camara/'
SOURCE = 'Fundación Canal – Ciclo de Música de Cámara'
VENUE = 'Auditorio de la Fundación Canal'
CITY = 'Madrid'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.6',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), '%d/%m/%Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('.elemento.elemento_extra'):
        title = clean_text(item.select_one('h3.titulo_2'))
        event_date = parse_date(clean_text(item.select_one('.fecha')))
        description = clean_text(item.select_one('.desplegable_descripcion')) or None
        if not title or not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': page_url,
            'time_from': None,
            'venue': VENUE,
            'city': CITY,
            'country_code': 'ES',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    seen_pages = set()

    for page_number in range(1, 101):
        page_url = SOURCE_URL if page_number == 1 else f'{SOURCE_URL}?page={page_number}#programa2'
        try:
            response = session.get(page_url, timeout=60)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to scrape chamber music archive page',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            if page_number == 1:
                raise
            break

        page_records = parse_page(response.text, page_url)
        signature = tuple((record['title'], record['date']) for record in page_records)
        if not page_records or signature in seen_pages:
            break
        seen_pages.add(signature)
        records.extend(page_records)

        soup = BeautifulSoup(response.text, 'html.parser')
        if not soup.select_one(f'a[href*="page={page_number + 1}"]'):
            break

    return sorted(records, key=lambda record: (record['date'], record['title']))


class FundacionCanalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fundacioncanal_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FundacionCanalComCrawler().run()


if __name__ == '__main__':
    main()
