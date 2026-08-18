import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.greeleyphil.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar?format=json')
SOURCE = 'Greeley Philharmonic Orchestra'
TIME_ZONE = ZoneInfo('America/Denver')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

SCHEDULE_RE = re.compile(
    r'^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?'
    r'(?:,?\s+(?P<year>\d{4}))?\s*(?:-|at)\s*'
    r'(?:(?P<time>\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)'
    r'(?:\s+Matinee)?\s*-\s*)?(?P<venue>.+)$',
    re.IGNORECASE,
)

CITY_NAMES = ('Fort Morgan', 'Loveland', 'Windsor', 'Greeley')


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', html.unescape(str(value)).replace('\xa0', ' ')).strip()


def parse_time(value):
    if not value:
        return None
    normalized = re.sub(r'\.', '', value).upper().strip()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_datetime(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, TIME_ZONE)


def body_text(body):
    soup = BeautifulSoup(body or '', 'html.parser')
    for node in soup.select('style, script, .summary-block, .button-block, .horizontalrule-block'):
        node.decompose()
    return clean_text(soup.get_text('\n', strip=True)) or None


def schedule_lines(body):
    soup = BeautifulSoup(body or '', 'html.parser')
    lines = []
    for node in soup.select('p'):
        for value in node.get_text('\n', strip=True).splitlines():
            text = clean_text(value)
            if SCHEDULE_RE.match(text):
                lines.append(text)
    return lines


def city_and_venue(value):
    venue = re.sub(r'\s+and\s+Livestream$', '', clean_text(value), flags=re.IGNORECASE)
    venue = venue.rstrip('.,* ')
    city = None
    for candidate in CITY_NAMES:
        if re.search(rf'\b{re.escape(candidate)}\b', venue, re.IGNORECASE):
            city = candidate
            break

    # The named churches are unambiguous first-party location evidence even
    # when the city is not repeated in the event's location field.
    if city is None:
        if 'Windsor First United Methodist' in venue:
            city = 'Windsor'
        elif 'King of Glory Lutheran' in venue:
            city = 'Loveland'
        elif any(name in venue for name in ('Generations Church', 'Trinity Episcopal',
                                             'First Congregational Church')):
            city = 'Greeley'

    if city:
        venue = re.sub(rf'(?:,\s*|\s+)({re.escape(city)})$', '', venue, flags=re.IGNORECASE)
        venue = re.sub(rf'^{re.escape(city)}\s+', '', venue, flags=re.IGNORECASE)
    return city, clean_text(venue)


def occurrence_records(item, description):
    start = event_datetime(item['startDate'])
    end = event_datetime(item.get('endDate', item['startDate']))
    lines = schedule_lines(item.get('body'))
    records = []

    for line in lines:
        match = SCHEDULE_RE.match(line)
        if not match:
            continue
        values = match.groupdict()
        year = int(values['year']) if values['year'] else start.year
        try:
            event_date = datetime.strptime(
                f"{values['month']} {values['day']} {year}", '%B %d %Y'
            ).date()
        except ValueError:
            continue
        city, venue = city_and_venue(values['venue'])
        if not city or not venue:
            continue
        records.append(make_record(
            item,
            event_date.isoformat(),
            parse_time(values['time']) or start.strftime('%H:%M'),
            venue,
            city,
            description,
        ))

    if records:
        return records

    # A multi-day collection item represents several performances. Without
    # parseable occurrence venues it cannot safely become a valid record.
    if start.date() != end.date():
        log_message(
            'Skipping multi-day event without parseable occurrences',
            event='crawler_event_skipped',
            level='warning',
            url=urljoin(SOURCE_URL, item.get('fullUrl', '')),
            error_type='UnparseableOccurrences',
        )
        return []

    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    address = clean_text(location.get('addressLine2'))
    city = next(
        (candidate for candidate in CITY_NAMES if re.search(rf'\b{candidate}\b', address, re.I)),
        None,
    )
    if not city or not venue or venue.lower() == city.lower():
        return []
    return [make_record(item, start.date().isoformat(), start.strftime('%H:%M'), venue, city,
                        description)]


def make_record(item, event_date, time_from, venue, city, description):
    return {
        'title': clean_text(item.get('title')),
        'date': event_date,
        'url': urljoin(SOURCE_URL, item.get('fullUrl', '')),
        'time_from': time_from,
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
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    payload = response.json()

    items = [*payload.get('upcoming', []), *payload.get('past', [])]
    records = []
    for item in items:
        if not clean_text(item.get('title')) or not item.get('startDate') or not item.get('fullUrl'):
            continue
        records.extend(occurrence_records(item, body_text(item.get('body'))))

    if not records:
        log_message(
            'No calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class GreeleyPhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='greeleyphil_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    GreeleyPhilOrgCrawler().run()


if __name__ == '__main__':
    main()
