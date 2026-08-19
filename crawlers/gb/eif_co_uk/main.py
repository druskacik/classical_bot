import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.eif.co.uk/'
SOURCE = 'Edinburgh International Festival'
LISTING_URL = urljoin(SOURCE_URL, 'whats-on')
CITY = 'Edinburgh'
TIMEZONE = ZoneInfo('Europe/London')
GENRES = ('Classical Music', 'Opera', 'Dance', 'Contemporary Music', 'Family')
COLLECTION = 'eif_events_production'
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
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def search_config(session):
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    component = soup.select_one('[search-key][search-nodes]')
    if not component:
        raise ValueError('EIF search configuration was not found')
    nodes = component.get('search-nodes', '').split(';')
    node = next((item.strip() for item in nodes if item.strip()), '')
    key = component.get('search-key', '').strip()
    if not node or not key:
        raise ValueError('EIF search configuration is incomplete')
    return f'https://{node}/multi_search', key


def catalogue_documents(session):
    endpoint, key = search_config(session)
    documents = []
    page = 1
    while True:
        search = {
            'collection': COLLECTION,
            'q': '*',
            'query_by': 'title',
            'per_page': 100,
            'page': page,
            'sort_by': 'firstInstanceTimestamp:asc',
            'filter_by': (
                'hideFromSearch: != true&&genre:=[' + ','.join(GENRES) + ']'
            ),
        }
        response = session.post(
            endpoint,
            params={'x-typesense-api-key': key},
            json={'searches': [search]},
            timeout=45,
        )
        response.raise_for_status()
        result = response.json()['results'][0]
        hits = result.get('hits', [])
        documents.extend(hit['document'] for hit in hits)
        if len(documents) >= result.get('found', 0) or not hits:
            break
        page += 1
    return documents


def detail_venue(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    for item in soup.select('.c-meta__item'):
        key = clean_text(item.select_one('.c-meta__key')).rstrip(':').lower()
        if key == 'venue':
            return clean_text(item.select_one('.c-meta__value'))
    return ''


def occurrence_timestamps(document):
    timestamps = document.get('instanceTimestamps') or []
    if not timestamps:
        # Spektrix removes elapsed or unavailable instances from its live array.
        # The index retains the first and last advertised occurrences.
        timestamps = [
            document.get('firstInstanceTimestamp'),
            document.get('lastInstanceTimestamp'),
        ]
    return sorted({value for value in timestamps if isinstance(value, (int, float)) and value > 0})


def document_records(session, document):
    title = clean_text(document.get('title'))
    uri = document.get('uri')
    if not title or not uri:
        return []
    url = urljoin(SOURCE_URL, uri)
    venue = detail_venue(session, url)
    if not venue:
        return []
    description = clean_text(document.get('text_content')) or clean_text(document.get('summary')) or None
    records = []
    for timestamp in occurrence_timestamps(document):
        start = datetime.fromtimestamp(timestamp, TIMEZONE)
        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': CITY,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    documents = catalogue_documents(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(document_records, session, document): document.get('uri')
            for document in documents
        }
        for future in as_completed(futures):
            uri = futures[future]
            try:
                records.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape EIF event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=urljoin(SOURCE_URL, uri or ''),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class EifCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eif_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    EifCoUkCrawler().run()


if __name__ == '__main__':
    main()
