import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.teatrofraschini.it/'
SOURCE = 'Teatro Fraschini di Pavia'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
DEFAULT_VENUE = 'Teatro Fraschini'
DEFAULT_CITY = 'Pavia'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup(['script', 'style', 'noscript']):
        node.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    start = event.get('start_date', '')
    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except (TypeError, ValueError):
        return None

    title = clean_text(event.get('title'))
    url = str(event.get('url') or '').strip()
    if not title or not url:
        return None

    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        venue_data = {}
    city = clean_text(venue_data.get('city')) or DEFAULT_CITY
    venue = clean_text(venue_data.get('venue')) or DEFAULT_VENUE
    if not city or not venue:
        return None

    time_from = None
    if not event.get('all_day') and re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', start):
        time_from = start[11:16]

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
    }


class TeatroFraschiniItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrofraschini_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'start_date': '2000-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
            'per_page': 50,
            'page': 1,
            'status': 'publish',
        }
        records = []

        while True:
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Teatro Fraschini events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=params['page'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events', [])
            for event_data in events:
                record = parse_event(event_data)
                if record:
                    records.append(record)

            total_pages = int(payload.get('total_pages') or 1)
            if params['page'] >= total_pages or not events:
                break
            params['page'] += 1

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    TeatroFraschiniItCrawler().run()


if __name__ == '__main__':
    main()
