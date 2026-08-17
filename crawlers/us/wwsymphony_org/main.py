import html
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wwsymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Walla Walla Symphony'
COUNTRY_CODE = 'US'
LOCAL_TIMEZONE = ZoneInfo('America/Los_Angeles')

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
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def add_json_format(url):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def city_from_location(location):
    address_line = clean_html((location or {}).get('addressLine2'))
    if not address_line:
        return ''

    match = re.match(r'^(.+?),\s*[A-Z]{2}(?:\s*,?\s*\d{5}(?:-\d{4})?)?$', address_line)
    if match:
        return match.group(1).strip(' ,')

    # A few first-party records contain only the city in addressLine2.
    if re.fullmatch(r"[A-Za-z .'-]+", address_line):
        return address_line.strip()
    return ''


def description_from_item(item):
    parts = []
    for field in ('excerpt', 'body'):
        value = clean_html(item.get(field))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def record_from_item(item):
    title = html.unescape(clean_html(item.get('title')))
    slug = str(item.get('urlId') or '').strip('/')
    location = item.get('location') or {}
    venue = clean_html(location.get('addressTitle'))
    city = city_from_location(location)

    try:
        starts_at = datetime.fromtimestamp(float(item['startDate']) / 1000, LOCAL_TIMEZONE)
    except (KeyError, TypeError, ValueError, OSError):
        return None

    if not title or not slug or not venue or not city:
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(EVENTS_URL + '/', slug),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': description_from_item(item),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    page_url = add_json_format(EVENTS_URL)
    visited_urls = set()
    records = []

    while page_url and page_url not in visited_urls:
        visited_urls.add(page_url)
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
        for item in items:
            record = record_from_item(item)
            if record:
                records.append(record)

        pagination = payload.get('pagination') or {}
        next_url = pagination.get('nextPageUrl') if pagination.get('nextPage') else None
        page_url = add_json_format(urljoin(SOURCE_URL, next_url)) if next_url else None

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    unique = {
        (item['url'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))


class WwSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wwsymphony_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_events()


def main():
    WwSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
