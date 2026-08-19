import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mobileopera.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Mobile Opera'

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
    value = str(value)
    text = (
        BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
        if '<' in value
        else value
    )
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def venue_data(value):
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, dict)), {})
    if not isinstance(value, dict):
        return '', ''
    return clean_text(value.get('venue')), clean_text(value.get('city'))


def description_location(description):
    """Recover the one archived event whose explicitly stated venue lacks API data."""
    match = re.search(
        r'\bat (?:the )?(Larkins Music Center)\s*,.{0,150}?\b(Mobile)\s*,\s*Alabama\b',
        description,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return '', ''
    return match.group(1), match.group(2)


def event_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    description = clean_text(event.get('description')) or None
    venue, city = venue_data(event.get('venue'))
    if (not venue or not city) and description:
        fallback_venue, fallback_city = description_location(description)
        venue = venue or fallback_venue
        city = city or fallback_city

    try:
        start = datetime.strptime(event.get('start_date', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    if not title or not url.startswith(('http://', 'https://')) or not venue or not city:
        return None

    time_from = None if event.get('all_day') else start.strftime('%H:%M')
    if description:
        stated_time = re.search(
            rf'\b{start.strftime("%B")}\s+{start.day},\s*{start.year}\s+at\s+'
            r'(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?',
            description,
            flags=re.IGNORECASE,
        )
        if stated_time:
            hour = int(stated_time.group(1)) % 12
            if stated_time.group(3).lower() == 'p':
                hour += 12
            time_from = f'{hour:02d}:{int(stated_time.group(2) or 0):02d}'
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_events(session):
    page = 1
    events = []
    while True:
        response = session.get(
            EVENTS_API_URL,
            params={
                'start_date': '1900-01-01',
                'end_date': '2100-12-31',
                'per_page': 50,
                'page': page,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        page_events = payload.get('events')
        if not isinstance(page_events, list):
            raise ValueError('Events API response has no event list')
        events.extend(page_events)

        total_pages = int(payload.get('total_pages') or 0)
        if page >= total_pages:
            return events
        page += 1


class MobileOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mobileopera_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events = fetch_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Mobile Opera events',
                event='crawler_request_failed',
                level='error',
                url=EVENTS_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = [record for event in events if (record := event_record(event))]
        if not records:
            log_message(
                'No valid Mobile Opera events found',
                event='crawler_empty_listing',
                level='warning',
                url=EVENTS_API_URL,
                record_count=0,
            )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    MobileOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
