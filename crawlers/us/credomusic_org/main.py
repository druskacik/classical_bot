import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.credomusic.org/'
LISTING_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Credo Music'
TIME_ZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def json_url(url):
    parts = urlsplit(urljoin(SOURCE_URL, url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def city_from_item(item):
    location = item.get('location') or {}
    address_line = clean_text(location.get('addressLine2'))
    if address_line:
        city = address_line.split(',', 1)[0].strip()
        if city:
            return city

    # One archived event omits addressLine2, but names both this venue and city.
    venue = clean_text(location.get('addressTitle'))
    title = clean_text(item.get('title'))
    if 'chicago' in venue.lower() or 'chicago' in title.lower():
        return 'Chicago'
    return ''


def item_to_record(item):
    title = clean_text(item.get('title'))
    full_url = item.get('fullUrl')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_item(item)
    start_timestamp = item.get('startDate')
    if not title or not full_url or not venue or not city or not start_timestamp:
        return None

    try:
        starts_at = datetime.fromtimestamp(start_timestamp / 1000, TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(item.get('body') or item.get('excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    next_url = json_url(LISTING_URL)
    seen_pages = set()
    seen_events = set()
    records = []

    while next_url and next_url not in seen_pages:
        seen_pages.add(next_url)
        response = session.get(next_url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in payload.get('upcoming', []) + payload.get('past', []):
            event_key = (item.get('id'), item.get('startDate'))
            if event_key in seen_events:
                continue
            seen_events.add(event_key)
            record = item_to_record(item)
            if record:
                records.append(record)

        next_page = (payload.get('pagination') or {}).get('nextPageUrl')
        next_url = json_url(next_page) if next_page else None

    if not records:
        log_message(
            'No scrapeable calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class CredoMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='credomusic_org',
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
    CredoMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
