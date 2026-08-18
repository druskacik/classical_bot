import html
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.indybaroque.org/'
SOURCE = 'Indianapolis Baroque Orchestra'
EVENTS_URL = urljoin(SOURCE_URL, 'events')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/151.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}

LOCAL_VENUES = {
    'newfields': ('Newfields', 'Indianapolis'),
    'park tudor school': ('Park Tudor School', 'Indianapolis'),
    'indiana history center': ('Indiana History Center', 'Indianapolis'),
    'christel dehaan fine arts center': (
        'Christel DeHaan Fine Arts Center, University of Indianapolis',
        'Indianapolis',
    ),
    "st. paul’s episcopal church": ("St. Paul’s Episcopal Church", 'Indianapolis'),
    "st. paul's episcopal church": ("St. Paul’s Episcopal Church", 'Indianapolis'),
    "st. christopher’s episcopal church": ("St. Christopher’s Episcopal Church", 'Carmel'),
    "st. christopher's episcopal church": ("St. Christopher’s Episcopal Church", 'Carmel'),
    'holy trinity lutheran church': ('Holy Trinity Lutheran Church', 'Chapel Hill'),
    'whiteland community high school': ('Whiteland Community High School', 'Whiteland'),
    'indiana state museum': ('Indiana State Museum', 'Indianapolis'),
    'indiana roof ballroom': ('Indiana Roof Ballroom', 'Indianapolis'),
}

CITY_LINE_RE = re.compile(
    r'^\s*([^,\n]+),\s*(?:[A-Z]{2}|[A-Za-z ]+?)(?:,?\s+\d{5}(?:-\d{4})?)?\s*$'
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\ufeff', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_datetime(milliseconds):
    try:
        return datetime.fromtimestamp(
            int(milliseconds) / 1000, ZoneInfo('America/Indiana/Indianapolis')
        )
    except (TypeError, ValueError, OSError):
        return None


def city_from_address(value):
    text = clean_text(value)
    match = CITY_LINE_RE.search(text)
    return match.group(1).strip() if match else None


def known_location(text):
    normalized = clean_text(text).lower().replace('’', "'")
    for marker, location in LOCAL_VENUES.items():
        if marker.replace('’', "'") in normalized:
            return location
    return None


def item_location(item, description):
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_address(location.get('addressLine2'))

    known = known_location(f'{venue}\n{description}')
    if not venue and known:
        venue = known[0]
    if not city and known:
        city = known[1]
    return venue or None, city


def learn_more_url(item):
    soup = BeautifulSoup(item.get('body') or '', 'html.parser')
    for link in soup.select('a[href]'):
        if clean_text(link.get_text(' ', strip=True)).lower() != 'learn more':
            continue
        url = urljoin(SOURCE_URL, link['href'])
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
            return url
    return None


def detail_text(session, item):
    url = learn_more_url(item)
    if not url:
        return '', None
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch event detail', event='crawler_item_failed',
            level='warning', url=url, error_type=type(error).__name__,
            error_message=str(error),
        )
        return '', None
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.select_one('main')
    return clean_text(main.get_text('\n', strip=True) if main else ''), url


def parse_item(session, item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    start = event_datetime(item.get('startDate'))
    if not title or not path or not start:
        return None

    description = clean_text(item.get('body') or item.get('excerpt'))
    venue, city = item_location(item, description)
    detail, detail_url = ('', None)
    if not venue or not city:
        detail, detail_url = detail_text(session, item)
        detail_location = known_location(detail)
        if detail_location:
            venue, city = detail_location
    if detail and len(detail) > len(description):
        description = detail
    if not venue or not city:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': detail_url or urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class IndyBaroqueOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='indybaroque_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(EVENTS_URL, params={'format': 'json'}, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch events feed', event='crawler_fetch_failed',
                level='error', url=EVENTS_URL, error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for bucket in ('upcoming', 'past'):
            for item in payload.get(bucket) or []:
                record = parse_item(session, item)
                if record:
                    records.append(record)
        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ))


def main():
    IndyBaroqueOrgCrawler().run()


if __name__ == '__main__':
    main()
