import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.americanwestsymphony.com/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'American West Symphony & Chorus'
COUNTRY_CODE = 'US'
TIME_ZONE = ZoneInfo('America/Denver')

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
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_city(location):
    line = clean_text((location or {}).get('addressLine2'))
    if not line:
        return ''
    return line.split(',', 1)[0].strip()


def parse_event(item):
    location = item.get('location') or {}
    title = clean_text(html.unescape(item.get('title') or ''))
    venue = clean_text(location.get('addressTitle'))
    city = parse_city(location)
    full_url = item.get('fullUrl') or ''
    start_ms = item.get('startDate')

    if not title or not venue or not city or not full_url or not start_ms:
        return None

    try:
        start = datetime.fromtimestamp(float(start_ms) / 1000, tz=TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': clean_text(item.get('body')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    payload = response.json()

    items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
    records = []
    for item in items:
        record = parse_event(item)
        if record:
            records.append(record)
        else:
            log_message(
                'Skipping event with incomplete required fields',
                event='crawler_event_skipped',
                level='warning',
                url=urljoin(SOURCE_URL, item.get('fullUrl') or ''),
            )

    if not records:
        log_message(
            'No concerts found in event collection',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class AmericanWestSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='americanwestsymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    AmericanWestSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
