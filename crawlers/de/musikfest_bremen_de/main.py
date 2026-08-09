import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musikfest-bremen.de/'
SOURCE = 'Musikfest Bremen'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
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
    text = html.unescape(text)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def fetch_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'page': page,
                'per_page': 50,
                'start_date': '1900-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
                'status': 'publish',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        page_events = payload.get('events') or []
        events.extend(page_events)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not page_events:
            break
        page += 1
    return events


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = str(event.get('start_date') or '')
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2}', start)

    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    country = clean_text(venue_data.get('country')).lower()
    if not title or not url or not match or not venue or not city:
        return None
    if country not in ('germany', 'deutschland', 'de'):
        return None

    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None if event.get('all_day') else f'{match.group(2)}:{match.group(3)}'
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MusikfestBremenDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musikfest_bremen_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        try:
            events = fetch_events(make_session())
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Musikfest Bremen events',
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
    MusikfestBremenDeCrawler().run()


if __name__ == '__main__':
    main()
