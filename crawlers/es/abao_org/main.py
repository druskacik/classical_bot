import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.abao.org/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'historico-de-temporadas/')
SOURCE = 'ABAO Bilbao Opera'
CITY = 'Bilbao'
MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5,
    'junio': 6, 'julio': 7, 'agosto': 8, 'septiembre': 9,
    'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def is_local_url(url):
    return urlparse(url).netloc in ('abao.org', 'www.abao.org')


def discover_listing_pages(session):
    """Return all current and archived season pages exposed by the site."""
    queue = [SOURCE_URL, ARCHIVE_URL]
    pages = set()
    visited = set()
    while queue:
        url = queue.pop()
        if url in visited:
            continue
        visited.add(url)
        soup = get_soup(session, url)
        for link in soup.select('a[href]'):
            href = urljoin(SOURCE_URL, link.get('href'))
            path = urlparse(href).path.rstrip('/') + '/'
            if not is_local_url(href) or '/temporada/' not in path:
                continue
            # Season pages are finite and linked from the current navigation or
            # the public archive. Avoid unrelated editorial pages in the same CPT.
            if re.search(r'(?:20\d{2}|\d{4})[-\u2011]\d{2}/$', path):
                pages.add(href)
    return sorted(pages)


def listing_occurrences(card, url):
    title = clean_text(card.select_one('.abao-performance__heading-link'))
    venue = clean_text(card.select_one('.abao-performance__location'))
    records = []
    if not title or not venue:
        return records
    for group in card.select('.abao-performance__date-group'):
        heading = clean_text(group.select_one('.abao-performance__date-group-header')).lower()
        match = re.search(r'([a-záéíóúñ]+)\s+(20\d{2})', heading)
        if not match or match.group(1) not in MONTHS:
            continue
        month = MONTHS[match.group(1)]
        year = int(match.group(2))
        days = re.findall(r'\b\d{1,2}\b', clean_text(group.select_one('.abao-performance__date-group-days')))
        for raw_day in days:
            try:
                event_date = datetime(year, month, int(raw_day)).date().isoformat()
            except ValueError:
                continue
            records.append({
                'title': title, 'date': event_date, 'url': url,
                'time_from': None, 'venue': venue, 'city': CITY,
                'country_code': 'ES', 'description': None,
                'source_url': SOURCE_URL, 'source': SOURCE,
            })
    return records


def discover_catalog(session):
    urls = set()
    fallback_records = []
    for listing_url in discover_listing_pages(session):
        try:
            soup = get_soup(session, listing_url)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape season page',
                event='crawler_page_failed',
                level='warning',
                url=listing_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for link in soup.select('a.abao-performance__heading-link[href]'):
            href = urljoin(SOURCE_URL, link.get('href'))
            if is_local_url(href):
                urls.add(href)
                card = link.find_parent(class_='abao-performance')
                if card:
                    fallback_records.extend(listing_occurrences(card, href))
    return sorted(urls), fallback_records


def detail_description(soup):
    sections = []
    for selector in (
        '.performance__section--presentation',
        '.performance__section--artistic-record',
        '.performance__section--synopsis',
    ):
        node = soup.select_one(selector)
        text = clean_text(node)
        if text:
            sections.append(text)
    return '\n\n'.join(sections) or None


def parse_detail(soup, url):
    title = clean_text(soup.select_one('h1.abao-page-heading__heading'))
    location = soup.select_one('.performance__meta-item--location')
    venue_node = location.select_one('.performance__meta-item-content') if location else None
    venue = clean_text(venue_node)
    calendar = soup.select_one('.abao-calendar[data-event-list]')
    if not title or not venue or calendar is None:
        return []

    try:
        events = json.loads(calendar.get('data-event-list') or '[]')
    except (TypeError, json.JSONDecodeError):
        return []

    description = detail_description(soup)
    records = []
    for event in events:
        raw_datetime = event.get('event_date_time')
        try:
            event_datetime = datetime.strptime(raw_datetime, '%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            continue
        records.append({
            'title': title,
            'date': event_datetime.date().isoformat(),
            'url': url,
            'time_from': event_datetime.strftime('%H:%M'),
            'venue': venue,
            'city': CITY,
            'country_code': 'ES',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    urls, fallback_records = discover_catalog(session)
    urls_with_detail_dates = set()
    descriptions = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(get_soup, session, url): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                soup = future.result()
                detail_records = parse_detail(soup, url)
                records.extend(detail_records)
                descriptions[url] = detail_description(soup)
                if detail_records:
                    urls_with_detail_dates.add(url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    for record in fallback_records:
        if record['url'] not in urls_with_detail_dates:
            record['description'] = descriptions.get(record['url'])
            records.append(record)
    unique_records = {
        (item['url'], item['date'], item['time_from']): item
        for item in records
    }
    return sorted(
        unique_records.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class AbaoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='abao_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    AbaoOrgCrawler().run()


if __name__ == '__main__':
    main()
