import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tallahasseesymphony.org/'
SOURCE = 'Tallahassee Symphony Orchestra'

# The public pages are protected by Cloudflare, but the site's first-party
# Events Calendar REST API is intentionally available to indexing clients.
API_URL = 'http://tallahasseesymphony.org/wp-json/tribe/events/v1/events'

HEADERS = {
    # Cloudflare redirects browser and search-engine user agents away from
    # query-bearing API URLs, while the site's machine-readable endpoint
    # explicitly serves command-line indexing clients.
    'User-Agent': 'curl/8.0',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    elif '<' in str(value) and '>' in str(value):
        value = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    else:
        value = str(value)
    value = html.unescape(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def fetch_catalogue(session):
    events = []
    seen_ids = set()
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 50,
                'page': page,
                # The API otherwise defaults to upcoming events. The archive
                # currently begins in 2022 and remains publicly available.
                'start_date': '2000-01-01',
                'end_date': '2100-12-31',
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        batch = payload.get('events') or []
        new_events = [event for event in batch if event.get('id') not in seen_ids]
        events.extend(new_events)
        seen_ids.update(event.get('id') for event in new_events)

        total_pages = int(payload.get('total_pages') or page)
        if page >= total_pages or not batch:
            return events
        if not new_events:
            raise ValueError('Event API pagination repeated a previously fetched page')
        page += 1


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') if isinstance(event.get('venue'), dict) else {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    start = clean_text(event.get('start_date'))
    match = re.fullmatch(
        r'(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?',
        start,
    )
    if not all((title, url, venue, city, match)):
        return None

    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None if event.get('all_day') else f'{match.group(2)}:{match.group(3)}'
    description = clean_text(event.get('description')) or clean_text(event.get('excerpt')) or None
    return {
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
    }


class TallahasseeSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tallahasseesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        # The calendar includes orchestra concerts alongside lunches and a
        # fundraising home tour. Its category coverage is too incomplete for
        # a safe direct-classical feed.
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
        try:
            events = fetch_catalogue(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Tallahassee Symphony event catalogue',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = [record for event in events if (record := parse_event(event))]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    TallahasseeSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
