import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatroallascala.org/it/index.html'
CALENDAR_URL = 'https://www.teatroallascala.org/it/calendario.html'
SOURCE = 'Teatro alla Scala'
VENUE = 'Teatro alla Scala'
CITY = 'Milano'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

# These are the site's own calendar classifications. Talks, museum visits,
# tours, and off-site events have separate classes and are intentionally not
# included. Family programming is retained because it contains staged opera,
# ballet, children's concerts, and other eligible performances. It also has
# some workshops, so the resulting candidate feed requires classification.
INCLUDED_TYPES = {
    'mcl-type-opera',
    'mcl-type-balletto',
    'mcl-type-concerti',
    'mcl-type-grandi_spettacoli_per_piccoli',
    'mcl-type-grandi_spettacoli_per_piccoli_e_famiglie',
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


def listing_record(link):
    if not INCLUDED_TYPES.intersection(link.get('class', [])):
        return None

    time_node = link.select_one('time[datetime]')
    title_node = link.select_one('.mcl-evt-title')
    href = (link.get('href') or '').strip()
    if not time_node or not title_node or not href:
        return None

    try:
        start = datetime.fromisoformat(time_node['datetime'])
    except (KeyError, ValueError):
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    url = urljoin(CALENDAR_URL, href)
    if not title or not url.startswith('https://www.teatroallascala.org/'):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': VENUE,
        'city': CITY,
        'country_code': 'IT',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []

    header = soup.select_one('.cnt__header')
    if header:
        container = header.find_parent(class_='container')
        text = clean_text(container)
        if text:
            parts.append(text)

    # Pages expose musical forces, casts, programmes, and synopses in these
    # stable, named content sections. Some pages legitimately omit one or more.
    for node in soup.select('#cast, #in_breve, #programma'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)

    if not parts:
        meta = soup.select_one('meta[name="description"][content]')
        text = clean_text(meta.get('content')) if meta else ''
        if text:
            parts.append(text)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    soup = get_soup(session, CALENDAR_URL)

    records_by_occurrence = {}
    for link in soup.select('a.mcl-evt-a[href]'):
        record = listing_record(link)
        if record:
            key = (record['url'], record['date'], record['time_from'])
            records_by_occurrence[key] = record

    records = list(records_by_occurrence.values())
    descriptions = {}
    urls = sorted({record['url'] for record in records})
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


class TeatroAllaScalaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatroallascala_org',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    TeatroAllaScalaOrgCrawler().run()


if __name__ == '__main__':
    main()
