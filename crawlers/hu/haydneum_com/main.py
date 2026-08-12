import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://haydneum.com/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Haydneum'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'hu-HU,hu;q=0.9,en;q=0.7',
}

COUNTRY_CODES = {
    'ausztria': 'AT',
    'austria': 'AT',
    'belgium': 'BE',
    'belgique': 'BE',
    'csehország': 'CZ',
    'czech republic': 'CZ',
    'france': 'FR',
    'franciaország': 'FR',
    'germany': 'DE',
    'magyarország': 'HU',
    'hungary': 'HU',
    'németország': 'DE',
    'románia': 'RO',
    'romania': 'RO',
    'slovakia': 'SK',
    'szlovákia': 'SK',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    else:
        text = html.unescape(text)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def api_events(session):
    page = 1
    while True:
        response = session.get(
            EVENTS_API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '2000-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
                'status': 'publish',
            },
            timeout=90,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events', [])
        if not events:
            break
        yield from events
        if page >= int(payload.get('total_pages') or page):
            break
        page += 1


def parse_country(venue):
    country = clean_text(venue.get('country')).casefold().rstrip('.')
    if country:
        return COUNTRY_CODES.get(country)
    # The API omits the country on many domestic venue records. Haydneum is a
    # Hungarian institution, and foreign tour venues observed in the feed do
    # carry an explicit country value.
    return 'HU'


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        return None

    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    country_code = parse_country(venue_data)
    try:
        starts_at = datetime.strptime(event.get('start_date', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None
    if not all((title, url, venue, city, country_code)):
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    records = []
    try:
        events = api_events(make_session())
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped Haydneum event with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=event.get('url'),
                )
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to scrape Haydneum events API',
            event='crawler_page_failed',
            level='error',
            url=EVENTS_API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    return sorted(
        records,
        key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']),
    )


class HaydneumComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='haydneum_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HU',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    HaydneumComCrawler().run()


if __name__ == '__main__':
    main()
