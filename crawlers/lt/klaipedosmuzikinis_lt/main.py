import re
from datetime import date as calendar_date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://klaipedosmuzikinis.lt/'
SOURCE = 'Klaipėdos valstybinis muzikinis teatras'
SITEMAPS = (
    'seasons-sitemap.xml',
    'seasons-guests-sitemap.xml',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'lt-LT,lt;q=0.9,en;q=0.5',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def season_urls(session):
    urls = set()
    for path in SITEMAPS:
        url = SOURCE_URL + path
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'xml')
            for node in soup.find_all('loc'):
                value = clean_text(node)
                if '/en/' not in value and urlparse(value).netloc == urlparse(SOURCE_URL).netloc:
                    urls.add(value)
        except requests.RequestException as error:
            log_message(
                'Failed to load season sitemap', event='crawler_feed_failed',
                level='warning', url=url, error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(urls)


def city_and_country(venue):
    normalized = venue.casefold()
    places = (
        ('gdansk', 'Gdańsk', 'PL'), ('wybrze', 'Gdańsk', 'PL'),
        ('tartu', 'Tartu', 'EE'), ('vanemu', 'Tartu', 'EE'),
        ('pernu', 'Pärnu', 'EE'), ('pärnu', 'Pärnu', 'EE'),
        ('vilni', 'Vilnius', 'LT'), ('lnobt', 'Vilnius', 'LT'),
        ('kaun', 'Kaunas', 'LT'), ('palang', 'Palanga', 'LT'),
        ('plung', 'Plungė', 'LT'), ('telši', 'Telšiai', 'LT'),
        ('akmen', 'Akmenė', 'LT'), ('mažeiki', 'Mažeikiai', 'LT'),
        ('nidos', 'Nida', 'LT'), ('nida', 'Nida', 'LT'),
    )
    for marker, city, country_code in places:
        if marker in normalized:
            return city, country_code
    # The theatre's named halls, foyers, and local festival venues are all in Klaipėda.
    return 'Klaipėda', 'LT'


def detail_description(session, url, cache):
    if url in cache:
        return cache[url]
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        body = soup.select_one('.event-content') or soup.select_one('main')
        description = clean_text(body) or None
    except requests.RequestException as error:
        log_message(
            'Failed to load event detail', event='crawler_detail_failed',
            level='warning', url=url, error_type=type(error).__name__,
            error_message=str(error),
        )
        description = None
    cache[url] = description
    return description


def parse_card(card, session, descriptions):
    title_node = card.select_one('.home-events__title')
    if not title_node:
        return None
    title = clean_text(title_node)
    url = title_node.get('href', '').strip()
    date_value = card.get('data-date', '')
    match = re.fullmatch(r'\s*(\d{4})\s+(\d{2})\s+(\d{2})\s*', date_value)
    details = card.select_one('.home-events__card > .d-flex.justify-content-between')
    if not match or not details or not title or not url:
        return None
    blocks = [clean_text(node) for node in details.find_all('div', recursive=False)]
    if len(blocks) < 2 or not blocks[0]:
        return None
    venue = blocks[0]
    time_match = re.search(r'([01]\d|2[0-3]):[0-5]\d', blocks[-1])
    city, country_code = city_and_country(venue)
    year, month, day = map(int, match.groups())
    try:
        date = calendar_date(year, month, day).isoformat()
    except ValueError:
        return None
    return {
        'title': title, 'date': date, 'url': url,
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue, 'city': city, 'country_code': country_code,
        'description': detail_description(session, url, descriptions),
        'source_url': SOURCE_URL, 'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    descriptions = {}
    for url in season_urls(session):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to load season page', event='crawler_feed_failed',
                level='warning', url=url, error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        soup = BeautifulSoup(response.text, 'html.parser')
        records.extend(
            record for card in soup.select('.event')
            if (record := parse_card(card, session, descriptions))
        )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class KlaipedosMuzikinisLtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='klaipedosmuzikinis_lt', source=SOURCE, source_url=SOURCE_URL,
        country_code='LT', upload_target='potential',
        columns=['title', 'date', 'url', 'time_from', 'venue', 'city',
                 'country_code', 'description', 'source_url', 'source'],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    KlaipedosMuzikinisLtCrawler().run()


if __name__ == '__main__':
    main()
