import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www1.wdr.de/orchester-und-chor/startseite/konzerte/index.html'
SITEMAP_URL = 'https://www1.wdr.de/index~sitemap.xml'
SOURCE = 'WDR Orchester und Chor'
EVENT_PATH = re.compile(r'/orchester-und-chor/.+/konzerte/termine/.+\.html$')
COUNTRY_CODE = re.compile(r'^[A-Za-z]{2}$')
CITY_COUNTRY_OVERRIDES = {
    'amsterdam': 'NL',
    'antwerpen': 'BE',
    'brüssel': 'BE',
    'bruxelles': 'BE',
    'london': 'GB',
    'luxemburg': 'LU',
    'luxembourg': 'LU',
    'paris': 'FR',
    'salzburg': 'AT',
    'wien': 'AT',
    'zürich': 'CH',
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
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=16,
        pool_maxsize=16,
        max_retries=Retry(
            total=3,
            backoff_factor=0.7,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'xml' if url.endswith('.xml') else 'html.parser')


def sitemap_locations(soup):
    return [clean_text(node) for node in soup.select('loc') if clean_text(node)]


def get_sitemap_locations(session, url):
    return sitemap_locations(get_soup(session, url))


def discover_event_urls(session):
    index = get_soup(session, SITEMAP_URL)
    sitemap_urls = sitemap_locations(index)
    event_urls = set()

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_sitemap_locations, session, url): url for url in sitemap_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                locations = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape WDR sitemap',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for location in locations:
                if EVENT_PATH.search(location):
                    event_urls.add(location)

    # The live calendar is a cheap fallback for newly published pages which have
    # not reached the sitemap yet.
    try:
        calendar = get_soup(session, SOURCE_URL)
        for link in calendar.select('a[href]'):
            url = urljoin(SOURCE_URL, link.get('href', ''))
            if EVENT_PATH.search(url):
                event_urls.add(url)
    except requests.RequestException as error:
        log_message(
            'Failed to scrape WDR live concert calendar',
            event='crawler_page_failed',
            level='warning',
            url=SOURCE_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    return sorted(event_urls)


def event_json(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def first_location(event):
    locations = event.get('location') or []
    if isinstance(locations, dict):
        locations = [locations]
    return next((item for item in locations if isinstance(item, dict)), {})


def detail_description(soup, fallback=None):
    parts = []
    for node in soup.select('.sectionZ .modParagraph'):
        value = clean_text(node)
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or clean_text(fallback) or None


def parse_event_page(soup, url):
    event = event_json(soup)
    if not event:
        return None

    title = clean_text(event.get('name'))
    location = first_location(event)
    address = location.get('address') or {}
    if not isinstance(address, dict):
        address = {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country_code = clean_text(address.get('addressCountry')).upper() or 'DE'
    country_code = CITY_COUNTRY_OVERRIDES.get(city.casefold(), country_code)
    if not title or not venue or not city or not COUNTRY_CODE.fullmatch(country_code):
        return None

    try:
        starts_at = datetime.fromisoformat(str(event.get('startDate', '')).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M') if 'T' in str(event.get('startDate')) else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': detail_description(soup, event.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_event_page(session, url):
    return parse_event_page(get_soup(session, url), url)


def get_concerts():
    session = make_session()
    urls = discover_event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(scrape_event_page, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape WDR concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda record: (
        record['date'], record['time_from'] or '', record['city'], record['title'], record['url'],
    ))


class Www1WdrDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='www1_wdr_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    Www1WdrDeCrawler().run()


if __name__ == '__main__':
    main()
