import re
from datetime import datetime, timezone
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orpheusnyc.org/'
EVENTS_URL = f'{SOURCE_URL}tickets-and-concerts'
SEARCH_URL = f'{SOURCE_URL}actions/orpheus-module/events/search-events'
SOURCE = 'Orpheus Chamber Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# These first-party categories contain recordings/streams or a fundraising
# reception rather than physical classical performances. Blank categories are
# retained because the site assigns that value to its touring concerts.
EXCLUDED_CATEGORIES = {'Livestream', 'Special Events'}
PLACEHOLDER_VENUE = re.compile(r'\b(?:rsvp|location details|online|on demand)\b', re.I)
US_LOCATION = re.compile(r'(?:^|,\s*)([^,]+),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$')


def clean_text(value):
    if not value:
        return ''
    text = unescape(str(value))
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def parse_location(value):
    location = clean_text(value)
    match = US_LOCATION.search(location)
    if not match:
        return None
    city = match.group(1).strip()
    if not city or any(char.isdigit() for char in city):
        return None
    return city, 'US'


def detail_description(session, url, subtitle):
    parts = []
    subtitle = clean_text(subtitle)
    if subtitle:
        parts.append(subtitle)

    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Orpheus concert detail',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return '\n\n'.join(parts) or None

    soup = BeautifulSoup(response.text, 'html.parser')
    description = soup.select_one('.concert-description')
    if description:
        text = clean_text(description)
        if text and text not in parts:
            parts.append(text)

    program_title = soup.select_one('.program-title')
    if program_title:
        program_column = program_title.parent
        text = clean_text(program_column)
        if text and text not in parts:
            parts.append(text)

    return '\n\n'.join(parts) or None


def event_record(session, event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue = clean_text(event.get('eventSpace'))
    location = parse_location(event.get('eventLocation'))
    date_time = clean_text(event.get('eventDate'))

    if (
        not title or not url or not venue or not location or not date_time
        or PLACEHOLDER_VENUE.search(venue)
    ):
        return None

    try:
        parsed = datetime.strptime(date_time, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None

    city, country_code = location
    return {
        'title': title,
        'date': parsed.date().isoformat(),
        'url': url,
        'time_from': parsed.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': detail_description(session, url, event.get('eventSubtitle')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_events(session):
    landing = session.get(EVENTS_URL, timeout=30)
    landing.raise_for_status()
    soup = BeautifulSoup(landing.text, 'html.parser')
    csrf = soup.select_one('meta[name="csrf-token"]')
    if not csrf or not csrf.get('content'):
        raise ValueError('Orpheus calendar did not provide a CSRF token')

    response = session.post(
        SEARCH_URL,
        json={
            'startDate': '2000-01-01',
            'endDate': f'{datetime.now(timezone.utc).year + 10}-12-31',
            'category': [],
        },
        headers={
            'X-CSRF-Token': csrf['content'],
            'X-Requested-With': 'XMLHttpRequest',
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get('success'):
        raise ValueError('Orpheus calendar API reported an unsuccessful search')
    return payload.get('events') or []


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    events = fetch_events(session)
    records = []
    for event in events:
        if clean_text(event.get('eventCategory')) in EXCLUDED_CATEGORIES:
            continue
        record = event_record(session, event)
        if record:
            records.append(record)

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue'], record['url']): record
        for record in records
    }
    result = sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['venue']),
    )
    if not result:
        log_message(
            'No valid Orpheus concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return result


class OrpheusNycOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orpheusnyc_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    OrpheusNycOrgCrawler().run()


if __name__ == '__main__':
    main()
