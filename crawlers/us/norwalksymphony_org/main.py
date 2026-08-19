import html
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.norwalksymphony.org/'
SOURCE = 'Norwalk Symphony Orchestra'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
TIME_ZONE = ZoneInfo('America/New_York')
DEFAULT_CITY = 'Norwalk'
DEFAULT_VENUE = 'Norwalk Concert Hall'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html,application/xhtml+xml,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def with_json_format(url):
    parsed = urlparse(urljoin(SOURCE_URL, url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunparse(parsed._replace(query=urlencode(query)))


def calendar_urls(session):
    """Discover published Squarespace event collections, including archives."""
    candidates = set()

    homepage = session.get(SOURCE_URL, timeout=45)
    homepage.raise_for_status()
    for link in BeautifulSoup(homepage.text, 'html.parser').select('a[href]'):
        path = urlparse(urljoin(SOURCE_URL, link.get('href'))).path
        if re.fullmatch(r'/20\d{6}(?:[-_][a-z-]+)?/?', path, re.IGNORECASE):
            candidates.add(urljoin(SOURCE_URL, path))

    sitemap = session.get(SITEMAP_URL, timeout=45)
    sitemap.raise_for_status()
    sitemap_soup = BeautifulSoup(sitemap.content, 'xml')
    for node in sitemap_soup.find_all('loc'):
        url = html.unescape(node.get_text(strip=True))
        parsed = urlparse(url)
        if parsed.netloc.lower() != urlparse(SOURCE_URL).netloc.lower():
            continue
        # Squarespace event URLs are /collection/YYYY/M/D/slug. Deriving the
        # collection path also finds a calendar that is no longer in the menu.
        match = re.match(r'^(/[^/]+)/20\d{2}/\d{1,2}/\d{1,2}/[^/]+/?$', parsed.path)
        if match:
            candidates.add(urljoin(SOURCE_URL, match.group(1)))

    calendars = []
    for candidate in sorted(candidates):
        response = session.get(with_json_format(candidate), timeout=45)
        response.raise_for_status()
        payload = response.json()
        if (payload.get('collection') or {}).get('typeName') == 'events':
            calendars.append(candidate)
    return calendars


def paginated_items(session, calendar_url):
    page_url = with_json_format(calendar_url)
    seen_pages = set()
    items = []

    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        payload = response.json()
        items.extend(payload.get('past') or [])
        items.extend(payload.get('upcoming') or [])
        items.extend(payload.get('items') or [])

        pagination = payload.get('pagination') or {}
        next_url = pagination.get('nextPageUrl') or pagination.get('nextPage')
        page_url = with_json_format(next_url) if next_url else None

    return items


def local_datetime(milliseconds):
    try:
        return datetime.fromtimestamp(float(milliseconds) / 1000, TIME_ZONE)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def city_from_location(location):
    address_line = clean_text(location.get('addressLine2'))
    city = address_line.split(',', 1)[0].strip() if address_line else ''
    return city or DEFAULT_CITY


def venue_from_location(location, city):
    address_line = clean_text(location.get('addressLine1'))
    if city.casefold() == DEFAULT_CITY.casefold() and re.search(
        r'\b125\s+East\s+Avenue\b', address_line, re.IGNORECASE
    ):
        return DEFAULT_VENUE

    venue = clean_text(location.get('addressTitle'))
    if venue and venue.casefold() != SOURCE.casefold():
        return venue
    return ''


def parse_item(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    start = local_datetime(item.get('startDate'))
    location = item.get('location') or {}
    city = city_from_location(location)
    venue = venue_from_location(location, city)
    description = clean_text(item.get('body')) or clean_text(item.get('excerpt'))

    if not title or not path or not start or not city or not venue:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    calendars = calendar_urls(session)
    for calendar_url in calendars:
        for item in paginated_items(session, calendar_url):
            record = parse_item(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped event with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=urljoin(SOURCE_URL, item.get('fullUrl') or ''),
                )

    if not calendars:
        log_message(
            'No published Squarespace event calendars found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class NorwalkSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='norwalksymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    NorwalkSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
