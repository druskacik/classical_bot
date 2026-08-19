import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.nationalsawdust.org/'
PERFORMANCES_URL = urljoin(SOURCE_URL, 'performances-prev')
SOURCE = 'National Sawdust'
VENUE = 'National Sawdust'
CITY = 'Brooklyn'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def listing_items(session):
    soup = get_soup(session, PERFORMANCES_URL)
    items = {}
    for link in soup.select('a.cms-wrapper[href*="/event/"]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        if not url.startswith(urljoin(SOURCE_URL, 'event/')):
            continue
        items[url] = {
            'url': url,
            'title': clean_text(link.select_one('[fs-cmsfilter-field="Title"]')),
            'date': clean_text(link.select_one('.category-title-date')),
            'description': clean_text(link.select_one('.cms_description')),
        }
    return list(items.values())


def parse_date(value):
    value = clean_text(value).replace(',', '')
    for pattern in ('%B %d %Y', '%b %d %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    value = re.sub(r'\s+', ' ', value)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def detail_description(soup, fallback=None):
    parts = []
    intro = clean_text(soup.select_one('.intro-description'))
    if intro:
        parts.append(intro)
    for block in soup.select('.column_text.w-richtext:not(.default-hidden)'):
        text = clean_text(block)
        if text and text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or clean_text(fallback) or None


def make_record(item, soup=None):
    soup = soup or BeautifulSoup('', 'html.parser')
    title = clean_text(soup.select_one('h1.header-title')) or item.get('title', '')
    details = [clean_text(node) for node in soup.select('.event-details')]

    event_date = None
    time_from = None
    for value in details:
        event_date = event_date or parse_date(value)
        time_from = time_from or parse_time(value)
    event_date = event_date or parse_date(item.get('date'))

    if not title or not event_date or not item.get('url'):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': item['url'],
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': detail_description(soup, item.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = listing_items(session)
    records = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, item['url']): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = make_record(item, future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape performance detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=item['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                record = make_record(item)
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class NationalSawdustOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nationalsawdust_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
    NationalSawdustOrgCrawler().run()


if __name__ == '__main__':
    main()
