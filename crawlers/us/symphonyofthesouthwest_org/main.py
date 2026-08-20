import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.symphonyofthesouthwest.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Symphony of the Southwest'
TIME_ZONE = ZoneInfo('America/Phoenix')

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
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def description_from_html(value):
    if not value:
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for node in soup.select('script, style, .sqs-block-button'):
        node.decompose()
    return clean_text(soup.get_text('\n', strip=True)) or None


def event_datetime(value):
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=TIME_ZONE)
    except (OSError, OverflowError, ValueError):
        return None


def event_location(event, description):
    location = event.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    address_line = clean_text(location.get('addressLine2'))
    city = clean_text(address_line.split(',', 1)[0]) if address_line else ''

    if venue and city:
        return venue, city

    # One archived event has a broken empty map marker, but its own event body
    # explicitly identifies the orchestra's home venue and city.
    location_evidence = (description or '').lower()
    if 'ikeda theater' in location_evidence and 'mesa arts center' in location_evidence:
        return 'Mesa Arts Center - Ikeda Theater', 'Mesa'
    return None, None


def make_record(event):
    title = clean_text(event.get('title'))
    path = clean_text(event.get('fullUrl'))
    start = event_datetime(event.get('startDate'))
    description = description_from_html(event.get('body'))
    venue, city = event_location(event, description)

    if not title or not path or not start or not venue or not city:
        return None

    url = urljoin(SOURCE_URL, path)
    if not url.startswith(('http://', 'https://')):
        return None

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


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CALENDAR_URL, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    payload = response.json()

    events = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
    records = []
    for event in events:
        record = make_record(event)
        if record:
            records.append(record)
        else:
            log_message(
                'Skipping concert with incomplete required fields',
                event='crawler_item_skipped',
                level='warning',
                url=urljoin(SOURCE_URL, clean_text(event.get('fullUrl'))),
            )

    if not records:
        log_message(
            'No concerts found in calendar feed',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class SymphonyOfTheSouthwestOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='symphonyofthesouthwest_org',
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
    SymphonyOfTheSouthwestOrgCrawler().run()


if __name__ == '__main__':
    main()
