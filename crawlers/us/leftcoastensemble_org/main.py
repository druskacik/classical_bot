import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.leftcoastensemble.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'left-coast-events-calendar')
SOURCE = 'Left Coast Chamber Ensemble'
TIMEZONE = ZoneInfo('America/Los_Angeles')

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
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    for node in soup.select('script, style, noscript'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def local_datetime(timestamp):
    try:
        return datetime.fromtimestamp(float(timestamp) / 1000, tz=TIMEZONE)
    except (TypeError, ValueError, OSError):
        return None


def city_from_location(location):
    address = clean_text((location or {}).get('addressLine2'))
    if not address:
        return ''
    return address.split(',', 1)[0].strip()


def parse_item(item):
    title = clean_text(item.get('title'))
    start = local_datetime(item.get('startDate'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    path = item.get('fullUrl') or ''
    url = urljoin(SOURCE_URL, path)

    if not title or not start or not venue or not city or not path:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(item.get('body')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    url = f'{CALENDAR_URL}?format=json'
    seen_pages = set()
    records = []

    while url and url not in seen_pages:
        seen_pages.add(url)
        response = session.get(url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
            record = parse_item(item)
            if record:
                records.append(record)

        pagination = payload.get('pagination') or {}
        next_path = pagination.get('nextPageUrl') if pagination.get('nextPage') else None
        if next_path:
            separator = '&' if '?' in next_path else '?'
            url = urljoin(SOURCE_URL, f'{next_path}{separator}format=json')
        else:
            url = None

    if not records:
        log_message(
            'No parseable calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class LeftCoastEnsembleOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='leftcoastensemble_org',
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
    LeftCoastEnsembleOrgCrawler().run()


if __name__ == '__main__':
    main()
