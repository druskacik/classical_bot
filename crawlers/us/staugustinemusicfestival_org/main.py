import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://staugustinemusicfestival.org/'
SOURCE = 'St. Augustine Music Festival'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

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
    soup = BeautifulSoup(str(value), 'html.parser')
    return re.sub(r'\s+', ' ', html.unescape(soup.get_text(' ', strip=True))).strip()


def parse_start(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def venue_fields(event, description):
    venue = event.get('venue')
    if isinstance(venue, dict):
        venue_name = clean_text(venue.get('venue'))
        city = clean_text(venue.get('city'))
        if venue_name and city:
            return venue_name, city

    # Several recent records omit the structured venue while naming it in the
    # body. Keep this deliberately narrow rather than guessing from an address.
    if re.search(r'\b(?:at|the)\s+(?:the\s+)?Waterworks\b', description, re.IGNORECASE):
        return 'The Waterworks', 'St. Augustine'
    return None, None


def parse_event(event):
    title = clean_text(event.get('title'))
    description = clean_text(event.get('description'))
    event_date, time_from = parse_start(event.get('start_date'))
    venue, city = venue_fields(event, description)
    url = event.get('url')
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class StAugustineMusicFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='staugustinemusicfestival_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'page': page,
                        'per_page': 50,
                        'start_date': '2000-01-01 00:00:00',
                        'end_date': '2100-12-31 23:59:59',
                    },
                    timeout=45,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch St. Augustine Music Festival events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events') or []
            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = payload.get('total_pages') or 1
            if page >= total_pages:
                break
            page += 1

        log_message(
            'Scraped St. Augustine Music Festival events',
            event='crawler_scrape_completed',
            url=API_URL,
            record_count=len(records),
        )
        return records


def main():
    return StAugustineMusicFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
