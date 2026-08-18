import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fmsymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'upcoming-events?format=json')
SOURCE = 'Fargo-Moorhead Symphony Orchestra'
DEFAULT_CITY = 'Fargo'
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


def local_datetime(milliseconds):
    try:
        return datetime.fromtimestamp(float(milliseconds) / 1000, TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None


def event_occurrences(item):
    start = local_datetime(item.get('startDate'))
    end = local_datetime(item.get('endDate'))
    if not start:
        return []

    occurrences = [(start.date().isoformat(), start.strftime('%H:%M'))]
    # Squarespace represents a two-performance concert as one event whose
    # start and end are the two advertised performance times.
    if end and end.date() != start.date():
        occurrences.append((end.date().isoformat(), end.strftime('%H:%M')))
    return occurrences


def city_from_location(location):
    address = clean_text(location.get('addressLine2'))
    if address:
        city = address.split(',', 1)[0].strip()
        if city:
            return city
    return DEFAULT_CITY


def venue_from_item(item, description):
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    if venue:
        return venue

    # Some entries omit the structured location but state the venue in the
    # opening line of the first-party event body.
    for line in description.splitlines()[:8]:
        match = re.search(r'([^|\n]*\bVenue\b)', line, re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return ''


def parse_item(item):
    title = clean_text(item.get('title'))
    description = clean_text(item.get('body')) or clean_text(item.get('excerpt'))
    venue = venue_from_item(item, description)
    location = item.get('location') or {}
    occurrences = event_occurrences(item)
    path = item.get('fullUrl')
    url = urljoin(SOURCE_URL, path) if path else ''

    if not title or not venue or not occurrences or not path:
        return []

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city_from_location(location),
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, event_time in occurrences
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    payload = response.json()

    items = (payload.get('past') or []) + (payload.get('upcoming') or [])
    records = []
    for item in items:
        parsed = parse_item(item)
        if not parsed:
            log_message(
                'Skipped event with incomplete required fields',
                event='crawler_item_skipped',
                level='warning',
                url=urljoin(SOURCE_URL, item.get('fullUrl') or ''),
            )
        records.extend(parsed)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FmSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fmsymphony_org',
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
    FmSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
