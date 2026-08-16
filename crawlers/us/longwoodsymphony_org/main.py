import html
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://longwoodsymphony.org/'
SOURCE = 'Longwood Symphony Orchestra'
EVENTS_URL = urljoin(SOURCE_URL, 'concerts')
TIME_ZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style, noscript'):
        node.decompose()
    text = html.unescape(soup.get_text('\n', strip=True))
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def with_json_format(url):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def parse_city(location):
    address_line = clean_html(location.get('addressLine2'))
    if not address_line:
        return ''
    return address_line.split(',', 1)[0].strip()


def parse_event(event):
    title = clean_html(event.get('title'))
    path = event.get('fullUrl')
    url = urljoin(SOURCE_URL, path) if path else ''
    location = event.get('location')
    if not isinstance(location, dict):
        return None

    venue = clean_html(location.get('addressTitle'))
    city = parse_city(location)
    try:
        start = datetime.fromtimestamp(event['startDate'] / 1000, tz=TIME_ZONE)
    except (KeyError, TypeError, ValueError, OSError):
        return None

    if not title or not url.startswith(('http://', 'https://')) or not venue or not city:
        return None

    description = clean_html(event.get('body')) or clean_html(event.get('excerpt')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


class LongwoodsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='longwoodsymphony_org',
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
        session = make_session()
        next_url = with_json_format(EVENTS_URL)
        seen_ids = set()
        records = []

        while next_url:
            try:
                response = session.get(next_url, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Longwood Symphony concert collection',
                    event='crawler_fetch_failed',
                    level='error',
                    url=next_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = (payload.get('upcoming') or []) + (payload.get('past') or [])
            for event in events:
                event_id = event.get('id')
                if event_id and event_id in seen_ids:
                    continue
                if event_id:
                    seen_ids.add(event_id)
                record = parse_event(event)
                if record:
                    records.append(record)

            next_path = (payload.get('pagination') or {}).get('nextPageUrl')
            next_url = with_json_format(urljoin(SOURCE_URL, next_path)) if next_path else None

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    LongwoodsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
