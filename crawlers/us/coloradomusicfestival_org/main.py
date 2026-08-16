import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://coloradomusicfestival.org/'
SOURCE = 'Colorado Music Festival'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_events(session):
    params = {
        'per_page': 50,
        'start_date': '1900-01-01 00:00:00',
        'end_date': '2100-12-31 23:59:59',
    }
    url = EVENTS_API
    events = []
    while url:
        response = session.get(url, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('events') or [])
        url = payload.get('next_rest_url')
        params = None
    return events


def fetch_detail_description(session, event):
    url = event.get('url') or ''
    if not url:
        return None
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    # The API description contains the event introduction. The detail block
    # adds the first-party artist and programme lists needed downstream.
    parts = [clean_text(event.get('description'))]
    details = clean_text(soup.select_one('.cmf_event_details'))
    if details and details not in parts:
        parts.append(details)
    return '\n\n'.join(part for part in parts if part) or None


def make_record(event, description=None):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    start = event.get('start_date') or ''
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2}', start)
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    country = clean_text(venue_data.get('country')).lower()
    if not all((title, url, match, venue, city)):
        return None
    if country not in ('united states', 'united states of america', 'us', 'usa'):
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}' if not event.get('all_day') else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ColoradoMusicFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='coloradomusicfestival_org',
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
        try:
            events = fetch_events(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Colorado Music Festival events',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        descriptions = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(fetch_detail_description, session, event): event
                for event in events if event.get('url')
            }
            for future in as_completed(futures):
                event = futures[future]
                try:
                    descriptions[event.get('id')] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Colorado Music Festival event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=event.get('url'),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = [
            make_record(event, descriptions.get(event.get('id')))
            for event in events
        ]
        records = [record for record in records if record]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ColoradoMusicFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
