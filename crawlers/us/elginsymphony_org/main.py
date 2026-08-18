import html
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.elginsymphony.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'concert-and-event-schedule')
SOURCE = 'Elgin Symphony Orchestra'
TIME_ZONE = ZoneInfo('America/Chicago')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
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


def json_url(url):
    """Preserve Squarespace's timestamp pagination while requesting JSON."""
    parts = urlsplit(urljoin(SOURCE_URL, url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['format'] = 'json'
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def city_from_location(location):
    address_line = clean_text((location or {}).get('addressLine2'))
    if not address_line:
        return ''
    return clean_text(address_line.split(',', 1)[0])


def parse_item(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)

    try:
        starts_at = datetime.fromtimestamp(item['startDate'] / 1000, TIME_ZONE)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return None

    if not title or not path or not venue or not city:
        return None

    description = clean_text(item.get('body')) or clean_text(item.get('excerpt')) or None
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    records = []
    seen_pages = set()
    url = json_url(CALENDAR_URL)

    while url and url not in seen_pages:
        seen_pages.add(url)
        response = session.get(url, timeout=60)
        response.raise_for_status()
        payload = response.json()

        for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
            record = parse_item(item)
            if record:
                records.append(record)

        next_page = (payload.get('pagination') or {}).get('nextPageUrl')
        url = json_url(next_page) if next_page else None

    if not records:
        log_message(
            'No parseable calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    else:
        log_message(
            'Calendar events parsed',
            event='crawler_parse_completed',
            url=CALENDAR_URL,
            record_count=len(records),
        )

    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))


class ElginSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='elginsymphony_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ElginSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
