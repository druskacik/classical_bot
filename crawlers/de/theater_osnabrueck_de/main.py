import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theater-osnabrueck.de/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalender/')
SOURCE = 'Theater Osnabrück'
CITY = 'Osnabrück'

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_record(container):
    article = container.select_one('article')
    link = container.select_one('a.btn-secondary[href*="/veranstaltung/"]')
    title_node = article.select_one('h2') if article else None
    venue = clean_text(container.get('data-sp-ort'))
    raw_date = clean_text(container.get('data-sp-day'))
    if not all((article, link, title_node, venue, raw_date)):
        return None

    try:
        event_date = datetime.strptime(raw_date, '%d-%m-%Y').date().isoformat()
    except ValueError:
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    # The visually hidden suffix contains the date and time for screen readers.
    hidden = title_node.select_one('.sr-only')
    hidden_text = clean_text(hidden.get_text(' ', strip=True)) if hidden else ''
    if hidden:
        hidden.extract()
        title = clean_text(title_node.get_text(' ', strip=True))
    time_match = re.search(r'\b(\d{1,2}):(\d{2})\b', hidden_text)
    if not time_match:
        info = clean_text(article.select_one('.info'))
        time_match = re.search(r'Beginn:\s*(\d{1,2}):(\d{2})', info)

    url = urljoin(SOURCE_URL, link.get('href'))
    if not title or not url:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': (
            f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
            if time_match else None
        ),
        'venue': venue,
        'city': CITY,
        'country_code': 'DE',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    main = soup.select_one('main#main-content')
    if not main:
        return None

    parts = []
    # The synopsis and production metadata are content modules before the
    # ticket-date module. Later modules contain cast, press and related works.
    for child in main.find_all(recursive=False):
        classes = set(child.get('class') or [])
        if 'mod-kaufen' in classes:
            break
        if 'mod-content' not in classes:
            continue
        text = clean_text(child.get_text('\n', strip=True))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, CALENDAR_URL)
    records = []
    for container in soup.select('.mod-teaser--kalender[data-sp-day][data-sp-ort]'):
        record = listing_record(container)
        if record:
            records.append(record)

    descriptions = {}
    urls = {record['url'] for record in records}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, url): url for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        record['description'] = descriptions.get(record['url'])
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class TheaterOsnabrueckDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theater_osnabrueck_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TheaterOsnabrueckDeCrawler().run()


if __name__ == '__main__':
    main()
