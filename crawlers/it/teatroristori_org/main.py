import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatroristori.org/'
CALENDAR_URL = f'{SOURCE_URL}calendario/'
SOURCE = 'Teatro Ristori'
VENUE = 'Teatro Ristori'
CITY = 'Verona'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gen': 1,
    'feb': 2,
    'mar': 3,
    'apr': 4,
    'mag': 5,
    'giu': 6,
    'lug': 7,
    'ago': 8,
    'set': 9,
    'ott': 10,
    'nov': 11,
    'dic': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]{3,})\s+(\d{4})', value.strip())
    if not match:
        return None
    month = MONTHS.get(match.group(2)[:3].casefold())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):(\d{2})\b', value)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def detail_description(soup):
    heading = soup.select_one('main h1.H1Titolo')
    if heading is None:
        return None

    main = heading.find_parent('main')
    if main is None:
        return None
    parts = []
    for editor in main.select('.accordion .Editor'):
        text = clean_text(editor)
        if text and text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def parse_card(card):
    link = card.select_one('a[href]')
    title_node = card.select_one('.TitoloEvento')
    image = card.select_one('img[alt]')
    date_node = card.select_one('.DataEvento')
    if link is None or date_node is None:
        return None

    url = link.get('href', '').strip()
    title = clean_text(image.get('alt')) if image else clean_text(title_node)
    event_date = parse_date(clean_text(date_node))
    if not title or not event_date or not url:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(card.select_one('.OraEvento'))),
        'venue': VENUE,
        'city': CITY,
        'country_code': 'IT',
        'description': clean_text(card.select_one('.DescrizioneEvento')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, CALENDAR_URL)
    records = [parse_card(card) for card in soup.select('.SingoloEvento')]
    records = [record for record in records if record]

    internal_records = [
        record for record in records
        if urlparse(record['url']).netloc.casefold() == 'www.teatroristori.org'
    ]
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(get_soup, session, record['url']): record
            for record in internal_records
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                description = detail_description(future.result())
                if description:
                    record['description'] = description
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Teatro Ristori event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']),
    )


class TeatroRistoriOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatroristori_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
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
        try:
            return scrape_concerts()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Teatro Ristori calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise


def main():
    TeatroRistoriOrgCrawler().run()


if __name__ == '__main__':
    main()
