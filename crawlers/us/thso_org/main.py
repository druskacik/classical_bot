import html
import re
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.thso.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Terre Haute Symphony Orchestra'
TIME_ZONE = ZoneInfo('America/Indiana/Indianapolis')

HEADERS = {
    'Accept': 'application/json,text/plain,*/*',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for node in soup.select('script, style, noscript'):
        node.decompose()
    text = html.unescape(soup.get_text('\n', strip=True))
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_location(location):
    address_line = clean_html((location or {}).get('addressLine2'))
    if not address_line:
        return None
    # Squarespace stores US localities as "City, ST ZIP".  Splitting only
    # at the first comma preserves multi-word and hyphenated city names.
    city = address_line.split(',', 1)[0].strip()
    return city or None


def parse_item(item):
    title = clean_html(item.get('title'))
    location = item.get('location') or {}
    venue = clean_html(location.get('addressTitle'))
    city = city_from_location(location)
    start_ms = item.get('startDate')
    url_id = str(item.get('urlId') or '').strip('/')
    if not title or not venue or not city or not start_ms or not url_id:
        return None

    try:
        start = datetime.fromtimestamp(float(start_ms) / 1000, tz=TIME_ZONE)
    except (TypeError, ValueError, OverflowError):
        return None

    # Older Squarespace records often sit a fraction of a second before the
    # advertised minute (for example 18:59:59.700 for a 19:00 event).
    start = (start + timedelta(seconds=30)).replace(second=0, microsecond=0)

    description_parts = [clean_html(item.get('excerpt')), clean_html(item.get('body'))]
    description = '\n\n'.join(part for part in description_parts if part) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(EVENTS_URL + '/', url_id),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def json_url(url):
    parts = urlsplit(urljoin(SOURCE_URL, url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def fetch_json(session, url):
    error = None
    for attempt in range(3):
        try:
            response = session.get(json_url(url), timeout=45)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as caught:
            error = caught
            if attempt < 2:
                time.sleep(1 + attempt)
    log_message(
        'Failed to fetch THSO events page',
        event='crawler_page_fetch_failed',
        level='error',
        url=url,
        error_type=type(error).__name__,
        error_message=str(error),
    )
    raise error


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    next_url = EVENTS_URL
    visited_pages = set()
    seen_items = set()
    records = []

    while next_url and next_url not in visited_pages:
        visited_pages.add(next_url)
        payload = fetch_json(session, next_url)
        for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
            item_id = item.get('id')
            if item_id in seen_items:
                continue
            seen_items.add(item_id)
            record = parse_item(item)
            if record:
                records.append(record)

        pagination = payload.get('pagination') or {}
        next_url = urljoin(SOURCE_URL, pagination['nextPageUrl']) if pagination.get('nextPage') else None

    if not records:
        log_message(
            'No valid THSO event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title'], item['url']))


class ThsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='thso_org',
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
        return scrape_concerts()


def main():
    ThsoOrgCrawler().run()


if __name__ == '__main__':
    main()
