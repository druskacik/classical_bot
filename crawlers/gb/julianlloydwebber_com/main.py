import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://julianlloydwebber.com/'
SOURCE = 'Julian Lloyd Webber'
SITEMAP_URL = urljoin(SOURCE_URL, 'wp-sitemap-posts-post-1.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2,
    'mar': 3, 'march': 3, 'apr': 4, 'april': 4,
    'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}
DATE_SLUG_RE = re.compile(
    r'/(?:[^/]+-)?(?:[0-3]?\d)(?:st|nd|rd|th)?-('
    + '|'.join(MONTHS)
    + r')-(?:19|20)\d{2}/?$',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    # A few hand-authored JSON-LD values contain a missing backslash in a
    # Unicode escape (for example ``Glu00f3r`` instead of ``Glór``).
    text = re.sub(
        r'(?<=[A-Za-z])u00([0-9a-fA-F]{2})(?=[A-Za-z])',
        lambda match: chr(int(match.group(1), 16)),
        text,
    )
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def iter_json_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from iter_json_objects(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get('@graph')
        if graph:
            yield from iter_json_objects(graph)


def event_objects(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for item in iter_json_objects(payload):
            item_type = item.get('@type')
            types = item_type if isinstance(item_type, list) else [item_type]
            if 'Event' in types:
                yield item


def parse_start(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None


def country_code(address):
    value = address.get('addressCountry') if isinstance(address, dict) else None
    if isinstance(value, dict):
        value = value.get('name')
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    aliases = {
        'UNITED KINGDOM': 'GB', 'UK': 'GB', 'GREAT BRITAIN': 'GB',
        'IRELAND': 'IE', 'REPUBLIC OF IRELAND': 'IE',
    }
    return aliases.get(normalized, normalized if re.fullmatch(r'[A-Z]{2}', normalized) else None)


def records_from_page(session, url):
    soup = get_soup(session, url)
    records = []
    for event in event_objects(soup):
        start = parse_start(event.get('startDate'))
        location = event.get('location')
        if isinstance(location, list):
            location = next((item for item in location if isinstance(item, dict)), {})
        if not isinstance(location, dict):
            location = {}
        address = location.get('address')
        if not isinstance(address, dict):
            address = {}

        title = clean_text(event.get('name'))
        venue = clean_text(location.get('name'))
        city = clean_text(address.get('addressLocality'))
        country = country_code(address)
        if not all((title, start, venue, city, country)):
            continue

        description = clean_text(event.get('description')) or None
        records.append({
            'title': title,
            'date': start.date().isoformat(),
            'url': url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': country,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def candidate_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.content, 'xml')
    urls = set()
    for loc in sitemap.find_all('loc'):
        url = clean_text(loc)
        if urlparse(url).netloc != urlparse(SOURCE_URL).netloc:
            continue
        # Event posts use a concrete occurrence date in their slug. The
        # Bach, Bows & Arrows tour pages follow this convention consistently.
        if DATE_SLUG_RE.search(url):
            urls.add(url)

    # The home page's first-party Tour Dates section is authoritative for the
    # current tour and protects against a future event slug format change.
    homepage = get_soup(session, SOURCE_URL)
    heading = next(
        (item for item in homepage.find_all(re.compile(r'^h[1-6]$'))
         if clean_text(item).casefold() == 'tour dates'),
        None,
    )
    if heading:
        container = heading.find_next(['ul', 'ol'])
        if container:
            for link in container.select('a[href]'):
                url = urljoin(SOURCE_URL, link['href'])
                if urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
                    urls.add(url)
    return urls


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = candidate_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(records_from_page, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Julian Lloyd Webber event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class JulianLloydWebberComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='julianlloydwebber_com',
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
    JulianLloydWebberComCrawler().run()


if __name__ == '__main__':
    main()
