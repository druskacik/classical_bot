import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operaphila.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'whats-on/events/')
SOURCE = 'Opera Philadelphia'
CITY = 'Philadelphia'
COUNTRY_CODE = 'US'
ARCHIVE_START = '2000-01-01T00:00'

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_listing_item(item):
    link = item.select_one('.detail h3 a[href]')
    venue_node = item.select_one('.detail .venue .tandp')
    start = (item.get('data-datetime') or '').strip()
    if not link or not venue_node or not start:
        return None

    try:
        start_at = datetime.strptime(start, '%Y-%m-%d %H:%M')
    except ValueError:
        return None

    time_and_venue = clean_text(venue_node.get_text(' ', strip=True))
    parts = re.split(r'\s*[|•]\s*', time_and_venue, maxsplit=1)
    venue = parts[1].strip() if len(parts) == 2 else ''
    # The site uses this literal value when it has not published a venue.
    if not venue or 'to be announced' in venue.lower():
        return None

    title = clean_text(link.get_text(' ', strip=True))
    url = urljoin(SOURCE_URL, link.get('href'))
    if not title or not url:
        return None

    intro = item.select_one('.detail .intro')
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': clean_text(intro.get_text(' ', strip=True)) if intro else None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def listing_records(session):
    records = []
    page = 1
    while True:
        soup = get_soup(
            session,
            EVENTS_URL,
            params={
                'altTemplate': 'ajaxEventlist',
                'more': 'true',
                'page': page,
                'start': ARCHIVE_START,
                'category': 0,
            },
        )
        items = soup.select('.event-item[data-datetime]')
        if not items:
            break
        for item in items:
            record = parse_listing_item(item)
            if record:
                records.append(record)

        listing = soup.select_one('.list-set')
        if not listing or listing.get('data-more', '').lower() != 'true':
            break
        next_page = listing.get('data-page')
        try:
            next_page = int(next_page)
        except (TypeError, ValueError):
            next_page = page + 1
        if next_page <= page:
            break
        page = next_page

    unique = {}
    for record in records:
        key = (record['url'], record['date'], record['time_from'])
        unique[key] = record
    return list(unique.values())


def detail_description(session, record):
    soup = get_soup(session, record['url'])
    heading = soup.select_one('h1.branded, h1.bigger')
    container = None
    if heading:
        container = heading.find_parent('div', class_='ibloc')
        if not container:
            container = heading.find_parent(id='bloc-synopsis')
    detail = clean_text(container.get_text('\n', strip=True)) if container else ''

    # Production pages expose one schema.org MusicEvent per performance. Its
    # description is usually empty, but retain it if the CMS starts filling it.
    schema_descriptions = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict) and entry.get('@type') == 'MusicEvent':
                value = clean_text(entry.get('description'))
                if value:
                    schema_descriptions.append(value)

    parts = [record.get('description') or '', detail, *schema_descriptions]
    result = []
    for part in parts:
        if part and part not in result:
            result.append(part)
    return '\n\n'.join(result) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = listing_records(session)

    records_by_url = {}
    for record in records:
        records_by_url.setdefault(record['url'], []).append(record)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_description, session, grouped[0]): url
            for url, grouped in records_by_url.items()
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                description = future.result()
                for record in records_by_url[url]:
                    record['description'] = description
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class OperaphilaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaphila_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    OperaphilaOrgCrawler().run()


if __name__ == '__main__':
    main()
