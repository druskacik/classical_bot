import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orkest.nl/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerten/')
API_URL = 'https://cms.orkest.nl/api/v2/pages/'
SOURCE = 'Nederlands Philharmonisch & Nederlands Kamerorkest'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}
_thread_state = threading.local()

COUNTRY_CODES = {
    'belgie': 'BE',
    'belgië': 'BE',
    'belgium': 'BE',
    'duitsland': 'DE',
    'germany': 'DE',
    'nederland': 'NL',
    'netherlands': 'NL',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(url, params=None):
    session = getattr(_thread_state, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _thread_state.session = session
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def listing_pages():
    pages = []
    offset = 0
    while True:
        payload = get_json(
            API_URL,
            params={
                'type': 'concert.ConcertDetailPage',
                'locale': 'nl',
                'limit': 20,
                'offset': offset,
            },
        )
        items = payload.get('items') or []
        pages.extend(items)
        total = (payload.get('meta') or {}).get('total_count', 0)
        if not items or len(pages) >= total:
            return pages
        offset += len(items)


def country_code(location):
    country = clean_text(location.get('country')).lower()
    if not country:
        return 'NL'
    return COUNTRY_CODES.get(country)


def description_from(detail):
    parts = []
    introduction = clean_text((detail.get('richtext_introduction') or {}).get('content'))
    if introduction:
        parts.append(introduction)

    for block in detail.get('content') or []:
        if block.get('block_type') != 'rich_text':
            continue
        text = clean_text((block.get('richtext') or {}).get('content'))
        if text and text not in parts:
            parts.append(text)

    program = (detail.get('meta_content') or {}).get('program') or {}
    lines = [clean_text(line) for line in program.get('lines') or []]
    lines = [line for line in lines if line]
    if lines:
        parts.append('Programma\n' + '\n'.join(lines))
    return '\n\n'.join(parts) or None


def make_record(detail):
    title = clean_text((detail.get('header') or {}).get('title'))
    if title.lower().startswith('seizoenspresentatie'):
        return None

    api_title = clean_text(detail.get('title'))
    date_match = re.search(r' - (\d{4}-\d{2}-\d{2})$', api_title)
    if not date_match:
        return None
    try:
        event_date = date.fromisoformat(date_match.group(1)).isoformat()
    except ValueError:
        return None

    meta = detail.get('meta') or {}
    location = (detail.get('meta_content') or {}).get('location') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(location.get('city'))
    code = country_code(location)
    path = meta.get('url')
    url = urljoin(SOURCE_URL, path) if path else ''
    if not title or not url or not venue or not city or not code:
        return None

    time_from = clean_text((detail.get('meta_header') or {}).get('start_time')) or None
    if time_from and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from):
        time_from = None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': description_from(detail),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_record(page):
    detail_url = (page.get('meta') or {}).get('detail_url')
    return make_record(get_json(detail_url))


def get_concerts():
    pages = listing_pages()
    records = []
    # Convert each response inside the worker so large CMS image structures are
    # discarded immediately instead of accumulating for the full catalogue.
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_record, page): page
            for page in pages
            if (page.get('meta') or {}).get('detail_url')
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=(page.get('meta') or {}).get('html_url'),
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


class OrkestNlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orkest_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OrkestNlCrawler().run()


if __name__ == '__main__':
    main()
