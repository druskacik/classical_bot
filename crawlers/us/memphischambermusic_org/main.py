import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://memphischambermusic.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Memphis Chamber Music Society'
TIME_ZONE = ZoneInfo('America/Chicago')

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
    for node in soup.select('script, style'):
        node.decompose()
    return re.sub(r'\s+', ' ', soup.get_text(' ', strip=True)).strip()


def city_from_location(location):
    address_line = location.get('addressLine2') or ''
    match = re.match(r'\s*([^,]+?)\s*,\s*[A-Z]{2}\b', address_line)
    return match.group(1).strip() if match else ''


def local_datetime(timestamp):
    if not isinstance(timestamp, (int, float)):
        return None
    return datetime.fromtimestamp(timestamp / 1000, tz=TIME_ZONE)


def event_record(item):
    title = clean_html(item.get('title'))
    url_id = str(item.get('urlId') or '').strip('/')
    location = item.get('location') or {}
    venue = clean_html(location.get('addressTitle'))
    city = city_from_location(location)
    starts_at = local_datetime(item.get('startDate'))

    if not all((title, url_id, venue, city, starts_at)):
        return None

    description = clean_html(item.get('body')) or clean_html(item.get('excerpt')) or None
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(CALENDAR_URL + '/', url_id),
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
    response = session.get(
        CALENDAR_URL,
        params={'format': 'json'},
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()

    records = []
    for section in ('upcoming', 'past'):
        items = payload.get(section) or []
        for item in items:
            record = event_record(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete required fields',
                    event='crawler_event_skipped',
                    level='warning',
                    url=urljoin(CALENDAR_URL + '/', str(item.get('urlId') or '')),
                    error_type='IncompleteEventData',
                )

    if not records:
        log_message(
            'No parseable concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class MemphisChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='memphischambermusic_org',
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
    MemphisChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
