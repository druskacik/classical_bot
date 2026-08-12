import html
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://teatrosocialecomo.it/'
SOURCE = 'Teatro Sociale di Como'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

COUNTRY_CODES = {
    'austria': 'AT',
    'francia': 'FR',
    'germania': 'DE',
    'italia': 'IT',
    'italy': 'IT',
    'svizzera': 'CH',
    'switzerland': 'CH',
}


def clean_text(value):
    if value is None:
        return ''
    raw = str(value)
    if '<' not in raw and '>' not in raw:
        return ' '.join(html.unescape(raw).split()).strip()
    soup = BeautifulSoup(raw, 'html.parser')
    for unwanted in soup.select('script, style, noscript'):
        unwanted.decompose()
    text = soup.get_text('\n', strip=True)
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def parse_country(value):
    normalized = clean_text(value).casefold()
    return COUNTRY_CODES.get(normalized)


def parse_event(event):
    title = clean_text(html.unescape(event.get('title', '')))
    url = clean_text(event.get('url'))
    start = clean_text(event.get('start_date'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    try:
        event_date = date.fromisoformat(start[:10]).isoformat()
    except (TypeError, ValueError):
        return None

    if not all((title, url, venue, city)):
        return None

    country_code = parse_country(venue_data.get('country'))
    if country_code is None:
        # The calendar belongs to a Como institution and its venues are Italian
        # unless an event's venue explicitly identifies another country.
        country_code = 'IT'

    time_from = None
    if not event.get('all_day') and len(start) >= 16:
        candidate = start[11:16]
        if candidate[:2].isdigit() and candidate[3:].isdigit() and candidate[2] == ':':
            hours, minutes = map(int, candidate.split(':'))
            if 0 <= hours <= 23 and 0 <= minutes <= 59:
                time_from = candidate

    description = clean_text(event.get('description')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class TeatroSocialeComoItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrosocialecomo_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'per_page': 50,
            'page': 1,
            'start_date': '2000-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
            'status': 'publish',
        }
        records = []

        while True:
            try:
                response = session.get(API_URL, params=params, timeout=60)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Teatro Sociale di Como events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=params['page'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events')
            if not isinstance(events, list):
                raise ValueError('Teatro Sociale di Como API returned no event list')

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = payload.get('total_pages', 1)
            if not isinstance(total_pages, int) or params['page'] >= total_pages:
                break
            params['page'] += 1

        return records


def main():
    return TeatroSocialeComoItCrawler().run()


if __name__ == '__main__':
    main()
