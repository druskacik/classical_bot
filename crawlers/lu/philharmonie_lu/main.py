import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.philharmonie.lu/en'
PROGRAMME_URL = f'{SOURCE_URL}/programme'
SOURCE = 'Philharmonie Luxembourg'
CITY = 'Luxembourg'

# The site is a mixed programme. Its first-party event-type filter removes
# workshops and guided tours, while retaining all concerts for classification.
FEEDS = (
    {'eventtype': 'concert'},
    {'eventtype': 'concert', 'pastevents': 'true'},
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_pages(session, feed):
    url = PROGRAMME_URL
    params = feed
    seen = set()
    while url and url not in seen:
        seen.add(url)
        soup = get_soup(session, url, params=params)
        yield soup
        button = soup.select_one('[js-hook-load-more-button][data-endpoint]')
        endpoint = button.get('data-endpoint') if button else None
        url = urljoin(SOURCE_URL, endpoint) if endpoint else None
        params = None


def parse_date(value):
    for pattern in ('%m/%d/%Y %I:%M:%S %p', '%m/%d/%Y'):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            pass
    return None


def listing_record(item):
    content = item.select_one('a.event-list-item__content[href]')
    title_node = content.select_one('h5') if content else None
    date_node = item.select_one('time.event-list-item__date-date[datetime]')
    time_nodes = item.select(
        '.event-list-item__date-time[datetime], '
        '.event-list-item__date-time time[datetime]'
    )
    venue_node = content.select_one('.event-list-item__label') if content else None
    if not all((content, title_node, date_node, venue_node)):
        return []

    title = clean_text(title_node)
    subtitle = clean_text(content.select_one('.event-list-item__subtitle'))
    if subtitle and subtitle.lower() not in title.lower():
        title = f'{title} – {subtitle}'
    event_date = parse_date(date_node.get('datetime') or '')
    venue = clean_text(venue_node)
    url = urljoin(SOURCE_URL, content.get('href'))
    if not title or not event_date or not venue or not url:
        return []

    base = {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': CITY,
        'country_code': 'LU',
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }
    times = list(dict.fromkeys(clean_text(node.get('datetime')) for node in time_nodes))
    times = [value for value in times if re.fullmatch(r'\d{2}:\d{2}', value)]
    if not times:
        return [base]
    return [{**base, 'time_from': value} for value in times]


def detail_description(session, url):
    soup = get_soup(session, url)
    column = soup.select_one('.event-detail-content__column')
    if not column:
        return None
    for node in column.select('button, .event-detail-content__header'):
        node.decompose()
    return clean_text(column) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records_by_url = {}
    for feed in FEEDS:
        for soup in listing_pages(session, feed):
            for item in soup.select('li.c-event-list-item'):
                for record in listing_record(item):
                    key = (record['url'], record['date'], record['time_from'])
                    records_by_url[key] = record

    records = list(records_by_url.values())
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(detail_description, session, record['url']): record
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
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class PhilharmonieLuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='philharmonie_lu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='LU',
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
    PhilharmonieLuCrawler().run()


if __name__ == '__main__':
    main()
