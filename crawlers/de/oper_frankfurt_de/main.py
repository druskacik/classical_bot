import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://oper-frankfurt.de/'
SOURCE = 'Oper Frankfurt'
CITY = 'Frankfurt am Main'

CATEGORY_URLS = [
    urljoin(SOURCE_URL, 'de/konzerte/'),
    urljoin(SOURCE_URL, 'de/konzerte/kammermusik/'),
    urljoin(
        SOURCE_URL,
        'de/konzerte/konzerte-der-paul-hindemith-orchesterakademie/',
    ),
    urljoin(SOURCE_URL, 'de/konzerte/soireen-des-opernstudios/'),
    urljoin(SOURCE_URL, 'de/konzerte/happy-new-ears/'),
    urljoin(SOURCE_URL, 'de/liederabende/liederabende/'),
]

MONTHS = {
    'januar': 1,
    'februar': 2,
    'maerz': 3,
    'märz': 3,
    'april': 4,
    'mai': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'dezember': 12,
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def new_session():
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
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_meta(value):
    text = clean_text(value)
    match = re.search(
        r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\s+(\d{4}),\s*'
        r'(\d{1,2})[.:](\d{2})\s*Uhr,\s*(.+)$',
        text,
    )
    if not match:
        return None
    day, month_name, year, hour, minute, venue = match.groups()
    month = MONTHS.get(month_name.lower())
    venue = clean_text(venue)
    if not month or not venue:
        return None
    try:
        event_date = date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None
    return event_date, f'{int(hour):02d}:{minute}', venue


def parse_listing(soup):
    records = []
    for element in soup.select('.repertoire-element-mini'):
        link = element.select_one('a[href*="/spielplan/"]')
        title_node = element.select_one('h3')
        meta = parse_meta(element.select_one('.meta'))
        if not link or not title_node or not meta:
            continue
        title = clean_text(title_node).replace('\n', ' ')
        url = urljoin(SOURCE_URL, link.get('href', ''))
        if not title or not url:
            continue
        event_date, time_from, venue = meta
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': CITY,
                'country_code': 'DE',
                'description': None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def detail_description(url):
    soup = get_soup(new_session(), url)
    body = soup.select_one('.veranstaltung-detail')
    return clean_text(body) or None


def get_concerts():
    session = new_session()
    records_by_url = {}
    for category_url in CATEGORY_URLS:
        try:
            soup = get_soup(session, category_url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert category',
                event='crawler_page_failed',
                level='warning',
                url=category_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for record in parse_listing(soup):
            records_by_url[record['url']] = record

    records = list(records_by_url.values())
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(detail_description, record['url']): record
            for record in records
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                record['description'] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class OperFrankfurtDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oper_frankfurt_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperFrankfurtDeCrawler().run()


if __name__ == '__main__':
    main()
