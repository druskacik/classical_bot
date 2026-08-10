import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.march.es/'
SOURCE = 'Fundación Juan March'
CANAL_URL = 'https://canal.march.es'
MUSIC_FEED_URL = (
    f'{CANAL_URL}/_next/data/canal-march/es/explorar/musica.json?category=musica'
)
VENUE = 'Auditorio de la Fundación Juan March'
CITY = 'Madrid'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


def iter_snippets(value):
    if isinstance(value, list):
        for item in value:
            yield from iter_snippets(item)
    elif isinstance(value, dict):
        if value.get('__type') == 'Snippet':
            yield value
        for item in value.values():
            yield from iter_snippets(item)


def listing_items(session):
    payload = get_json(session, MUSIC_FEED_URL)
    category = payload['pageProps']['categoryStaticInfo']['categoryData']
    allowed_types = {'Concierto', 'Teatro musical de Cámara'}
    items = {}
    for item in iter_snippets(category):
        media = item.get('mainContent') or {}
        alias = media.get('urlAlias')
        if alias and item.get('type') in allowed_types:
            items[alias] = item
    return list(items.items())


def detail_data(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    # The server omits a charset and requests otherwise assumes ISO-8859-1.
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, 'html.parser')
    node = soup.select_one('#__NEXT_DATA__')
    if node is None or not node.string:
        raise ValueError('Detail page has no Next.js data')
    payload = json.loads(node.string)
    return payload['props']['pageProps']['mediaSheetStaticInfo']['mediaContent']


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('Europe/Madrid'))
    return parsed.astimezone(ZoneInfo('Europe/Madrid'))


def make_record(detail, url):
    # The archive also contains performances made with partner institutions.
    # Only Madrid act URLs identify events at the Foundation's own auditorium.
    act_url = detail.get('actUrl') or ''
    if '/es/madrid/' not in act_url:
        return None

    title = clean_text(detail.get('title'))
    starts_at = parse_datetime(detail.get('date'))
    if not title or starts_at is None:
        return None

    description_parts = [clean_text(detail.get('description'))]
    works = clean_text(detail.get('worksBy'))
    if works:
        description_parts.append(f'Programa\n{works}')
    description = '\n\n'.join(part for part in description_parts if part) or None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': VENUE,
        'city': CITY,
        'country_code': 'ES',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = listing_items(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for alias, _item in items:
            url = f'{CANAL_URL}/es/coleccion/{alias}'
            futures[executor.submit(detail_data, session, url)] = url

        for future in as_completed(futures):
            url = futures[future]
            try:
                record = make_record(future.result(), url)
            except (KeyError, TypeError, ValueError, requests.RequestException) as error:
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


class MarchEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='march_es',
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
    MarchEsCrawler().run()


if __name__ == '__main__':
    main()
