import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.symphonyofthemountains.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events?format=json')
SOURCE = 'Symphony of the Mountains'
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
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style, noscript'):
        node.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_location(location):
    address_line = clean_html(location.get('addressLine2'))
    if not address_line:
        return ''
    # Squarespace stores this as "City, State, ZIP".
    return address_line.split(',', 1)[0].strip()


def description_from_item(item):
    parts = []
    for field in ('excerpt', 'body'):
        value = clean_html(item.get(field))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def record_from_item(item):
    title = clean_html(item.get('title'))
    path = item.get('fullUrl') or item.get('urlId')
    url = urljoin(SOURCE_URL, path or '')
    location = item.get('location') or {}
    venue = clean_html(location.get('addressTitle'))
    city = city_from_location(location)

    try:
        starts_at = datetime.fromtimestamp(int(item['startDate']) / 1000, tz=TIMEZONE)
    except (KeyError, TypeError, ValueError, OSError):
        return None

    if not title or not path or not venue or not city:
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description_from_item(item),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    payload = response.json()

    items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
    records = []
    skipped_count = 0
    for item in items:
        record = record_from_item(item)
        if record:
            records.append(record)
        else:
            skipped_count += 1

    if skipped_count:
        log_message(
            'Skipped events without a usable date, venue, or city',
            event='crawler_records_skipped',
            level='warning',
            url=EVENTS_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No valid events found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class SymphonyOfTheMountainsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='symphonyofthemountains_org',
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
        return scrape_events()


def main():
    SymphonyOfTheMountainsOrgCrawler().run()


if __name__ == '__main__':
    main()
