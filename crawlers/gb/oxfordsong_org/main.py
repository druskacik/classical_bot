import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://oxfordsong.org/'
SOURCE = 'Oxford International Song Festival'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
PAST_URL = urljoin(SOURCE_URL, 'events/past')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(node):
    if node is None:
        return ''
    value = node.get_text(' ', strip=True) if hasattr(node, 'get_text') else str(node)
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def event_links(soup):
    links = set()
    for anchor in soup.select('main a[href*="/events/"]'):
        url = urljoin(SOURCE_URL, anchor.get('href', '')).split('#', 1)[0]
        path = urlparse(url).path.rstrip('/')
        if (
            re.fullmatch(r'/events/[^/]+', path)
            and path != '/events/past'
            and not re.fullmatch(r'/events/p\d+', path)
        ):
            links.add(url)
    return links


def catalogue_pages(soup):
    pages = {EVENTS_URL}
    for anchor in soup.select('main a[href]'):
        url = urljoin(SOURCE_URL, anchor.get('href', '')).rstrip('/')
        if re.fullmatch(r'https://oxfordsong\.org/events/p\d+', url):
            pages.add(url)
    return pages


def music_event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload.get('@graph', []) if isinstance(payload, dict) else []
        for node in nodes:
            event_types = node.get('@type', []) if isinstance(node, dict) else []
            if isinstance(event_types, str):
                event_types = [event_types]
            if 'MusicEvent' in event_types:
                return node
    return None


def place_name_and_city(location):
    if not isinstance(location, dict):
        return None, None
    raw_name = clean_text(location.get('name'))
    address = location.get('address')
    locality = clean_text(address.get('addressLocality')) if isinstance(address, dict) else ''
    lines = [clean_text(line) for line in str(location.get('name', '')).splitlines() if clean_text(line)]
    venue = lines[0] if lines else raw_name
    if not locality:
        locality = next((line for line in lines[1:] if line.lower() == 'oxford'), '')
    if not locality and len(lines) >= 3 and re.search(
        r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', lines[-1], re.IGNORECASE
    ):
        locality = lines[-2]
    # Oxford Song's venue records consistently include Oxford in their postal address.
    if not locality and re.search(r'\bOxford\b', raw_name, re.IGNORECASE):
        locality = 'Oxford'
    return venue or None, locality or None


def parse_event(soup, page_url):
    schema = music_event_schema(soup)
    if not schema or not schema.get('startDate'):
        return None
    try:
        start = datetime.fromisoformat(schema['startDate'].replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    venue, city = place_name_and_city(schema.get('location'))
    title = clean_text(soup.select_one('main h1'))
    canonical_url = schema.get('url') or schema.get('mainEntityOfPage') or page_url
    if not all((title, venue, city, canonical_url)):
        return None

    description_parts = []
    main_description = soup.select_one('.event-content .main-description')
    if main_description:
        description_parts.append(clean_text(main_description))
    for programme in soup.select('.programme-toggle + .os-accordion-content'):
        text = clean_text(programme)
        if text:
            description_parts.append(f'Programme: {text}')
    description = ' '.join(dict.fromkeys(description_parts)) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': canonical_url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class OxfordSongOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oxfordsong_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def fetch(self, session, url):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser')

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        current = self.fetch(session, EVENTS_URL)
        detail_urls = set()
        for page_url in sorted(catalogue_pages(current)):
            soup = current if page_url == EVENTS_URL else self.fetch(session, page_url)
            detail_urls.update(event_links(soup))

        # The past index contains season/festival overview pages. Those overview
        # pages link to the concrete archived occurrences, which are the records
        # we want; the overview itself is deliberately not emitted.
        past = self.fetch(session, PAST_URL)
        overview_urls = event_links(past)
        for overview_url in sorted(overview_urls):
            try:
                detail_urls.update(event_links(self.fetch(session, overview_url)))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Oxford Song archive overview',
                    event='crawler_item_failed', level='warning', url=overview_url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self.fetch, session, url): url for url in detail_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = parse_event(future.result(), url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Oxford Song event',
                        event='crawler_item_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


def main():
    OxfordSongOrgCrawler().run()


if __name__ == '__main__':
    main()
