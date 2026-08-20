import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tarriverlive.com/'
LISTING_URL = urljoin(SOURCE_URL, 'upcoming-events')
SOURCE = 'Tar River Orchestra & Chorus'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def warmup_data(html):
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.find('script', id='wix-warmup-data')
    if not node or not node.string:
        return {}
    try:
        return json.loads(node.string)
    except json.JSONDecodeError:
        return {}


def find_event_objects(value):
    """Yield Wix event dictionaries without depending on generated component IDs."""
    if isinstance(value, dict):
        if all(key in value for key in ('title', 'slug', 'scheduling', 'location')):
            yield value
            return
        for child in value.values():
            yield from find_event_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from find_event_objects(child)


def listing_events(html):
    events = {}
    for event in find_event_objects(warmup_data(html)):
        slug = clean_text(event.get('slug'))
        if slug:
            events[slug] = event
    return list(events.values())


def rich_text(value):
    parts = []

    def visit(node):
        if isinstance(node, dict):
            text = clean_text(node.get('textData', {}).get('text'))
            if text:
                parts.append(text)
            for child in node.get('nodes', []):
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return clean_text(''.join(parts))


def event_from_detail(html, slug):
    candidates = [
        event for event in find_event_objects(warmup_data(html))
        if clean_text(event.get('slug')) == slug
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda event: len(rich_text(event.get('longDescription'))))


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def record_from_event(event, url):
    title = clean_text(event.get('title'))
    scheduling = event.get('scheduling') or {}
    event_date = parse_date(scheduling.get('startDateFormatted'))
    location = event.get('location') or {}
    full_address = location.get('fullAddress') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(full_address.get('city'))
    country_code = clean_text(full_address.get('country')).upper()
    if not all((title, event_date, url, venue, city, country_code)):
        return None

    long_description = rich_text(event.get('longDescription'))
    description = long_description or clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(scheduling.get('startTimeFormatted')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    events = listing_events(response.text)

    records = []
    for summary in events:
        slug = clean_text(summary.get('slug'))
        url = urljoin(SOURCE_URL, f'event-details/{slug}')
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            event = event_from_detail(detail_response.text, slug) or summary
        except requests.RequestException as error:
            log_message(
                'Event detail request failed; using listing data',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            event = summary

        record = record_from_event(event, url)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class TarRiverLiveComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tarriverlive_com',
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
    TarRiverLiveComCrawler().run()


if __name__ == '__main__':
    main()
