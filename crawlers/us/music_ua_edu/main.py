import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://music.ua.edu/'
SOURCE = 'University of Alabama School of Music'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_events(session):
    """Fetch the complete first-party calendar archive in stable API pages."""
    events = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API,
            params={
                'start_date': '2000-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
                'status': 'publish',
                'per_page': 50,
                'page': page,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        page_events = payload.get('events')
        if not isinstance(page_events, list):
            raise ValueError('Events API response has no events list')
        events.extend(page_events)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            return events
        page += 1


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    start_value = event.get('start_date')
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None

    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    country = clean_text(venue_data.get('country'))
    if country and country.lower() not in {'united states', 'united states of america', 'usa', 'us'}:
        return None

    try:
        starts_at = datetime.fromisoformat(start_value)
    except (TypeError, ValueError):
        return None

    if not title or not url or not venue or not city:
        return None
    if not str(url).startswith(('http://', 'https://')):
        return None

    description = clean_text(event.get('description'))
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    try:
        events = fetch_events(session)
    except (requests.RequestException, ValueError, TypeError) as error:
        log_message(
            'Failed to fetch University of Alabama School of Music events',
            event='crawler_fetch_failed',
            level='error',
            url=EVENTS_API,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = []
    skipped_count = 0
    for event in events:
        try:
            record = parse_event(event)
        except (AttributeError, TypeError, ValueError) as error:
            log_message(
                'Failed to parse University of Alabama School of Music event',
                event='crawler_item_failed',
                level='warning',
                url=event.get('url', EVENTS_API) if isinstance(event, dict) else EVENTS_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            skipped_count += 1
            continue
        if record:
            records.append(record)
        else:
            skipped_count += 1

    if skipped_count:
        log_message(
            'Skipped events without a complete US venue',
            event='crawler_items_skipped',
            level='info',
            url=EVENTS_API,
            record_count=skipped_count,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MusicUaEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='music_ua_edu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MusicUaEduCrawler().run()


if __name__ == '__main__':
    main()
