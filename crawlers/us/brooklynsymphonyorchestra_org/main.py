import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brooklynsymphonyorchestra.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Brooklyn Symphony Orchestra'
TIMEZONE = ZoneInfo('America/New_York')

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
    soup = BeautifulSoup(value, 'html.parser')
    for node in soup.select('script, style, noscript'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def json_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def city_from_location(location):
    address = (location or {}).get('addressLine2') or ''
    if re.search(r'\bbrooklyn\b', address, re.I):
        return 'Brooklyn'
    city = address.split(',', 1)[0].strip()
    if city and city.lower() not in {'ny', 'new york'}:
        return city
    # The collection is the orchestra's Brooklyn performance archive. Some
    # older entries name a Brooklyn venue but omit the city from its address.
    return 'Brooklyn'


def description_from_item(item):
    parts = []
    for field in ('excerpt', 'body'):
        text = clean_html(item.get(field))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def record_from_item(item):
    title = clean_html(item.get('title'))
    path = item.get('fullUrl')
    start = item.get('startDate')
    location = item.get('location') or {}
    venue = clean_html(location.get('addressTitle'))
    city = city_from_location(location)
    if not title or not path or not start or not venue or not city:
        return None

    try:
        starts_at = datetime.fromtimestamp(float(start) / 1000, tz=TIMEZONE)
    except (TypeError, ValueError, OverflowError, OSError):
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'description': description_from_item(item),
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    next_url = json_url(EVENTS_URL)
    seen_pages = set()
    seen_items = set()
    records = []
    skipped = 0

    while next_url and next_url not in seen_pages:
        seen_pages.add(next_url)
        response = session.get(next_url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
            item_key = item.get('id') or item.get('fullUrl')
            if not item_key or item_key in seen_items:
                continue
            seen_items.add(item_key)
            record = record_from_item(item)
            if record:
                records.append(record)
            else:
                skipped += 1

        next_path = (payload.get('pagination') or {}).get('nextPageUrl')
        next_url = json_url(next_path) if next_path else None

    log_message(
        'Squarespace event catalogue scraped',
        event='crawler_scrape_completed',
        url=EVENTS_URL,
        record_count=len(records),
        skipped_count=skipped,
        page_count=len(seen_pages),
    )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BrooklynSymphonyOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brooklynsymphonyorchestra_org',
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
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BrooklynSymphonyOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
