import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.londonchambermusic.org.uk/'
SOURCE = 'London Chamber Music Society'
LISTING_URLS = (f'{SOURCE_URL}what-s-on', f'{SOURCE_URL}past-concerts')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    else:
        value = html.unescape(str(value))
        if '<' in value and '>' in value:
            value = BeautifulSoup(value, 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def event_urls(soup):
    urls = set()
    for anchor in soup.select('a[href*="/event-details-registration/"]'):
        url = anchor.get('href', '').split('?', 1)[0]
        parsed = urlparse(url)
        if (
            parsed.netloc == 'www.londonchambermusic.org.uk'
            and parsed.path.startswith('/event-details-registration/')
        ):
            urls.add(url.split('?', 1)[0])
    return urls


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            nodes = candidate.get('@graph', [candidate])
            if not isinstance(nodes, list):
                nodes = [nodes]
            for node in nodes:
                event_types = node.get('@type', []) if isinstance(node, dict) else []
                if isinstance(event_types, str):
                    event_types = [event_types]
                if 'Event' in event_types or 'MusicEvent' in event_types:
                    return node
    return None


def city_from_address(address):
    address = clean_text(address)
    match = re.search(r'\bLondon\b', address, re.IGNORECASE)
    return 'London' if match else None


def parse_event(soup, page_url):
    schema = event_schema(soup)
    if not schema:
        return None
    try:
        start = datetime.fromisoformat(str(schema.get('startDate', '')).replace('Z', '+00:00'))
    except ValueError:
        return None

    location = schema.get('location') or {}
    if not isinstance(location, dict):
        return None
    venue = clean_text(location.get('name'))
    address = location.get('address') or ''
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
    else:
        city = city_from_address(address)
    title = clean_text(schema.get('name'))
    canonical = soup.select_one('link[rel="canonical"]')
    url = clean_text(canonical.get('href')) if canonical else page_url
    description = clean_text(schema.get('description')) or None
    if not all((title, venue, city, url)):
        return None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LondonChamberMusicOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='londonchambermusic_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def fetch(self, session, url):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return response

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = set()
        for listing_url in LISTING_URLS:
            response = self.fetch(session, listing_url)
            urls.update(event_urls(BeautifulSoup(response.text, 'html.parser')))
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self.fetch, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    response = future.result()
                    record = parse_event(BeautifulSoup(response.text, 'html.parser'), response.url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch London Chamber Music event',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped London Chamber Music page without complete event data',
                        event='crawler_item_skipped', level='warning', url=url,
                    )
        return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


def main():
    LondonChamberMusicOrgUkCrawler().run()


if __name__ == '__main__':
    main()
