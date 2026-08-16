import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://classicaltahoe.org/'
SOURCE = 'Classical Tahoe'
API_URL = 'https://classicaltahoe.org/wp-json/tribe/events/v1/events'
DEFAULT_VENUE = 'Classical Tahoe Ricardi Pavilion at University of Nevada, Reno at Lake Tahoe'
DEFAULT_CITY = 'Incline Village'

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
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    # Old event descriptions contain unrendered WPBakery shortcodes.
    text = re.sub(r'\[/?[a-zA-Z_][^\]]*\]', ' ', text)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    start = event.get('start_date_details') or {}

    try:
        event_date = date(
            int(start['year']), int(start['month']), int(start['day'])
        ).isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    hour = start.get('hour')
    minute = start.get('minutes')
    time_from = None
    if not event.get('all_day') and hour is not None and minute is not None:
        try:
            time_from = f'{int(hour):02d}:{int(minute):02d}'
        except (TypeError, ValueError):
            time_from = None

    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    # A few archived home performances have an empty venue object. One current
    # recital has "Incline Village" incorrectly entered as the venue name.
    # The site's venue-specific calendar and footer establish the pavilion as
    # the default; records with an explicit touring venue are never replaced.
    if not venue:
        venue = DEFAULT_VENUE
    if not city:
        city = DEFAULT_CITY
    if venue.casefold() == city.casefold():
        venue = DEFAULT_VENUE

    if not title or not url or not venue or not city:
        return None

    description = clean_text(event.get('description')) or None
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


class ClassicalTahoeOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='classicaltahoe_org',
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
        records = []
        page = 1

        while True:
            params = {
                'per_page': 50,
                'page': page,
                'start_date': '2000-01-01',
                'end_date': '2100-12-31',
                'status': 'publish',
            }
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Classical Tahoe events',
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
                raise ValueError('Classical Tahoe API response has no events list')

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            if not payload.get('next_rest_url'):
                break
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ClassicalTahoeOrgCrawler().run()


if __name__ == '__main__':
    main()
