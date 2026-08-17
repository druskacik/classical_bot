import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sdems.org/'
SOURCE = 'San Diego Early Music Society'
API_URL = 'https://sdems.org/wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup.select('style, script, .tribe-events-schedule, .tribe-block__event-price'):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_description_location(description):
    """Recover a venue from a clearly formatted US address in the event body."""
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    address_pattern = re.compile(
        r'\b(?P<city>[A-Za-z][A-Za-z .\'-]+),\s*CA\s+\d{5}(?:-\d{4})?\b',
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        match = address_pattern.search(line)
        if not match or index < 1:
            continue
        venue = lines[index - 1]
        if not re.search(r'\d', line) or len(venue) > 180:
            continue
        return venue, match.group('city').strip()
    return None


def parse_event(event):
    title = clean_text(event.get('title'))
    event_url = event.get('url')
    parsed_datetime = parse_datetime(event.get('start_date'))
    description = clean_text(event.get('description'))

    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        venue_data = {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    if not venue or not city:
        fallback = parse_description_location(description)
        if fallback:
            venue, city = fallback

    if not title or not event_url or not parsed_datetime or not venue or not city:
        return None

    event_date, time_from = parsed_datetime
    return {
        'title': title,
        'date': event_date,
        'url': event_url,
        'time_from': None if event.get('all_day') else time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SdemsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sdems_org',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            params = {
                'per_page': 50,
                'page': page,
                'start_date': '1900-01-01',
                'end_date': '2100-12-31',
                'status': 'publish',
            }
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch SDEMS events API',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events')
            if not isinstance(events, list):
                raise ValueError('SDEMS events API returned an invalid events payload')

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = payload.get('total_pages', 1)
            if not isinstance(total_pages, int) or page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SdemsOrgCrawler().run()


if __name__ == '__main__':
    main()
