import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.spartanburgphilharmonic.org/'
SOURCE = 'Spartanburg Philharmonic'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
LOCAL_TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

# The event collection sometimes omits its structured location. These series
# have consistently used the same venue, including on adjacent events whose
# structured locations are populated.
SERIES_VENUES = {
    'Zimmerli Series': 'Twichell Auditorium',
    'Classics Series': 'Twichell Auditorium',
    'Youth Orchestra': 'Twichell Auditorium',
    'Espresso Series': 'Chapman Cultural Center',
    'Bluegrass Series': 'Chapman Cultural Center',
    'Music Sandwiched In': 'Spartanburg County Public Library',
}


def clean_text(html):
    if not html:
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    for element in soup(['script', 'style']):
        element.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_body_venue(description):
    patterns = (
        r'(?im)^location:\s*([^\n(]+)',
        r'(?im)^venue:\s*([^\n(]+)',
        r'(?im)^\s*[^\n|]{0,30}\|\s*([^\n|]+(?:auditorium|cultural center|library|church|room))\s*$',
    )
    for pattern in patterns:
        match = re.search(pattern, description)
        if match:
            venue = match.group(1).strip(' |,-')
            if venue:
                return venue
    return None


def event_venue(item, description):
    location = item.get('location') or {}
    venue = (location.get('addressTitle') or '').strip()
    if venue:
        return venue

    venue = parse_body_venue(description)
    if venue:
        return venue

    categories = item.get('categories') or []
    for category in categories:
        if category in SERIES_VENUES:
            return SERIES_VENUES[category]
    return None


def event_dates(start, end):
    current = start.date()
    last = end.date()
    while current <= last:
        yield current
        current += timedelta(days=1)


def parse_item(item):
    title = clean_text(item.get('title'))
    full_url = item.get('fullUrl')
    start_ms = item.get('startDate')
    end_ms = item.get('endDate') or start_ms
    if not title or not full_url or not isinstance(start_ms, (int, float)):
        return []

    try:
        start = datetime.fromtimestamp(start_ms / 1000, LOCAL_TIMEZONE)
        end = datetime.fromtimestamp(end_ms / 1000, LOCAL_TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return []

    description = clean_text(item.get('body') or item.get('excerpt')) or None
    venue = event_venue(item, description or '')
    if not venue:
        return []

    records = []
    for event_date in event_dates(start, end):
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': urljoin(SOURCE_URL, full_url),
            'time_from': start.strftime('%H:%M') if event_date == start.date() else None,
            'venue': venue,
            'city': 'Spartanburg',
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class SpartanburgPhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='spartanburgphilharmonic_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        next_url = f'{EVENTS_URL}?format=json'
        seen_items = set()
        records = []

        while next_url:
            try:
                response = session.get(next_url, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Spartanburg Philharmonic events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=next_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for item in payload.get('upcoming', []) + payload.get('past', []):
                item_id = item.get('id')
                if not item_id or item_id in seen_items:
                    continue
                seen_items.add(item_id)
                records.extend(parse_item(item))

            page_url = (payload.get('pagination') or {}).get('nextPageUrl')
            if page_url:
                separator = '&' if '?' in page_url else '?'
                next_url = urljoin(SOURCE_URL, f'{page_url}{separator}format=json')
            else:
                next_url = None

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SpartanburgPhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
