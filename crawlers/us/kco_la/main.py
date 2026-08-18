import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kco.la/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Kaleidoscope Chamber Orchestra'
COUNTRY_CODE = 'US'
LOCAL_TIMEZONE = ZoneInfo('America/Los_Angeles')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*\|\s*'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.IGNORECASE,
)
ADDRESS_RE = re.compile(
    r'\b(?:Street|St\.?|Avenue|Ave\.?|Boulevard|Blvd\.?|Drive|Dr\.?|'
    r'Road|Rd\.?)\s*,?\s+([A-Za-z][A-Za-z .]+?),\s*CA\s+\d{5}(?:-\d{4})?\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', html.unescape(str(value)).replace('\xa0', ' ')).strip()


def body_lines(body):
    soup = BeautifulSoup(body or '', 'html.parser')
    return [clean_text(line) for line in soup.get_text('\n', strip=True).splitlines() if clean_text(line)]


def body_description(body):
    lines = body_lines(body)
    return '\n'.join(lines) or None


def parse_date(value):
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    normalized = clean_text(value).upper().replace(' ', '')
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def city_from_address(address):
    address = clean_text(address)
    parts = [part.strip() for part in address.split(',')]
    for index, part in enumerate(parts):
        if re.fullmatch(r'CA(?:\s+\d{5}(?:-\d{4})?)?', part, re.IGNORECASE) and index:
            candidate = clean_text(parts[index - 1])
            if not re.search(r'\d', candidate):
                return candidate

    match = ADDRESS_RE.search(address)
    return clean_text(match.group(1)) if match else None


def expanded_performances(item):
    """Expand collection entries whose body advertises several tour performances."""
    lines = body_lines(item.get('body'))
    occurrences = []
    for index, line in enumerate(lines):
        match = DATE_TIME_RE.fullmatch(line)
        if not match:
            continue

        event_date = parse_date(match.group(1))
        time_from = parse_time(match.group(2))
        venue = clean_text(lines[index + 1]) if index + 1 < len(lines) else ''
        address = clean_text(lines[index + 2]) if index + 2 < len(lines) else ''
        city = city_from_address(address)
        if event_date and time_from and venue and city:
            occurrences.append((event_date, time_from, venue, city))

    # A normal event body often repeats its one structured date. Only override the
    # collection metadata when the page explicitly contains multiple performances.
    return occurrences if len(occurrences) > 1 else []


def structured_occurrence(item):
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_address(location.get('addressLine2'))
    try:
        local_start = datetime.fromtimestamp(item['startDate'] / 1000, LOCAL_TIMEZONE)
    except (KeyError, TypeError, ValueError, OSError):
        return None

    if not venue or not city:
        return None
    return local_start.date().isoformat(), local_start.strftime('%H:%M'), venue, city


def item_records(item):
    title = clean_text(item.get('title'))
    path = clean_text(item.get('fullUrl'))
    url = urljoin(SOURCE_URL, path)
    if not title or not path or not url.startswith(('http://', 'https://')):
        return []

    performances = expanded_performances(item)
    if not performances:
        occurrence = structured_occurrence(item)
        performances = [occurrence] if occurrence else []

    description = body_description(item.get('body'))
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from, venue, city in performances
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    payload = response.json()

    items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
    records = []
    for item in items:
        parsed = item_records(item)
        if not parsed:
            log_message(
                'Skipped event with incomplete occurrence metadata',
                event='crawler_event_skipped',
                level='warning',
                url=urljoin(SOURCE_URL, clean_text(item.get('fullUrl'))),
            )
        records.extend(parsed)

    if not records:
        log_message(
            'No concerts found in events feed',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda record: (record['date'], record['time_from'] or '', record['title']))


class KcoLaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kco_la',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    KcoLaCrawler().run()


if __name__ == '__main__':
    main()
