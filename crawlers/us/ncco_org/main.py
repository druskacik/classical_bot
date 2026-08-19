import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ncco.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concert-season')
FEED_URL = f'{LISTING_URL}?format=json'
SOURCE = 'New Century Chamber Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'^\*?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4}):?$',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'^(\d{1,2})(?::(\d{2}))?\s*([ap]m)\*?$', re.IGNORECASE)
STOP_HEADINGS = {
    'FEATURED ARTIST',
    'FEATURED ARTISTS',
    'PROGRAM',
    'PROGRAM:',
    'WHAT YOU’LL HEAR',
    "WHAT YOU'LL HEAR",
}
SKIP_LINES = {'BUY TICKETS', 'GET TICKETS', 'DATES & VENUES'}
CITY_NAMES = (
    'Belvedere Tiburon',
    'Mountain View',
    'Rohnert Park',
    'San Francisco',
    'San Rafael',
    'Berkeley',
    'Stanford',
    'Vallejo',
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def body_lines(html):
    soup = BeautifulSoup(html or '', 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    return [clean_text(value) for value in soup.stripped_strings if clean_text(value)]


def parse_date(value):
    match = DATE_RE.match(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.match(clean_text(value))
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    try:
        return datetime.strptime(
            f'{hour}:{minute or "00"} {meridiem}', '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None


def venue_and_city(value):
    value = clean_text(value).rstrip('*').strip()
    city = next((name for name in CITY_NAMES if re.search(rf'\b{re.escape(name)}\*?$', value)), None)
    if not city:
        return None, None

    before_city = re.sub(rf',?\s*{re.escape(city)}\*?$', '', value).strip(' ,')
    venue = re.split(r',\s*\d(?![^()]*\))', before_city, maxsplit=1)[0].strip(' ,')
    if not venue:
        return None, None
    return venue, city


def shared_location(lines):
    for index, line in enumerate(lines):
        if line.rstrip(':').upper() != 'LOCATION':
            continue
        parts = []
        for candidate in lines[index + 1:index + 4]:
            if candidate.upper() in STOP_HEADINGS or parse_date(candidate):
                break
            parts.append(candidate)
            venue, city = venue_and_city(', '.join(parts))
            if venue and city:
                return venue, city
    return None, None


def performance_records(item):
    lines = body_lines(item.get('body'))
    try:
        start = next(index for index, line in enumerate(lines) if line.upper() == 'DATES & VENUES') + 1
    except StopIteration:
        return []

    shared_venue, shared_city = shared_location(lines[start:])
    records = []
    index = start
    while index < len(lines):
        if lines[index].upper() in STOP_HEADINGS:
            break
        event_date = parse_date(lines[index])
        if not event_date:
            index += 1
            continue

        time_from = None
        venue = city = None
        cursor = index + 1
        while cursor < len(lines) and not parse_date(lines[cursor]):
            candidate = lines[cursor]
            if candidate.upper() in STOP_HEADINGS:
                break
            if candidate.rstrip(':').upper() == 'LOCATION':
                break
            time_from = time_from or parse_time(candidate)
            if candidate.upper() not in SKIP_LINES and not candidate.startswith('*'):
                parsed_venue, parsed_city = venue_and_city(candidate)
                if parsed_venue and parsed_city:
                    venue, city = parsed_venue, parsed_city
                    break
            cursor += 1

        venue = venue or shared_venue
        city = city or shared_city
        if venue and city:
            records.append((event_date, time_from, venue, city))
        index += 1
    return records


def description_from_item(item):
    lines = body_lines(item.get('body'))
    unwanted = {'BUY TICKETS', 'GET TICKETS', 'SUBSCRIBE AND SAVE'}
    values = []
    for line in lines:
        if line.upper() in unwanted or line.startswith('#block-'):
            continue
        if line not in values:
            values.append(line)
    return '\n'.join(values) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(FEED_URL, timeout=45)
    response.raise_for_status()
    payload = response.json()
    items = [*payload.get('upcoming', []), *payload.get('past', [])]

    records = []
    for item in items:
        title = clean_text(item.get('title'))
        path = item.get('fullUrl')
        if not title or not path:
            continue
        url = urljoin(SOURCE_URL, path)
        description = description_from_item(item)
        for event_date, time_from, venue, city in performance_records(item):
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    if not records:
        log_message(
            'No concert performances found',
            event='crawler_empty_listing',
            level='warning',
            url=FEED_URL,
            record_count=0,
        )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class NccoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ncco_org',
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
    NccoOrgCrawler().run()


if __name__ == '__main__':
    main()
