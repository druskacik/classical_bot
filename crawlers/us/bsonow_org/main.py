import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bsonow.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Bakersfield Symphony Orchestra'
TIME_ZONE = ZoneInfo('America/Los_Angeles')

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
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_datetime(milliseconds):
    try:
        return datetime.fromtimestamp(int(milliseconds) / 1000, TIME_ZONE)
    except (TypeError, ValueError, OverflowError):
        return None


def city_from_location(location):
    address_line = (location or {}).get('addressLine2') or ''
    city = address_line.split(',', 1)[0].strip()
    return city or None


def event_to_record(event):
    title = html.unescape(str(event.get('title') or '')).strip()
    start = event_datetime(event.get('startDate'))
    location = event.get('location') or {}
    venue = html.unescape(str(location.get('addressTitle') or '')).strip()
    city = city_from_location(location)
    path = event.get('fullUrl') or ''
    url = urljoin(SOURCE_URL, path)

    if not title or not start or not venue or not city or not path:
        log_message(
            'Skipping calendar event with incomplete required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url or CALENDAR_URL,
            has_title=bool(title),
            has_date=bool(start),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return None

    description_parts = []
    for field in ('excerpt', 'body'):
        text = clean_html(event.get(field))
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CALENDAR_URL, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    payload = response.json()

    events = [*payload.get('past', []), *payload.get('upcoming', [])]
    records = []
    for event in events:
        record = event_to_record(event)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No concerts found in calendar feed',
            event='crawler_empty_listing',
            level='warning',
            url=response.url,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class BsonowOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bsonow_org',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BsonowOrgCrawler().run()


if __name__ == '__main__':
    main()
