import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://addaalicante.es/'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'ADDA Auditorio de Alicante'
CITY = 'Alicante'
DEFAULT_VENUE = 'ADDA'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'es-ES,es;q=0.9',
}

FUTURE_YEARS = 5


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, page=1):
    response = session.get(
        EVENTS_API,
        params={
            'start_date': '2000-01-01',
            # The API mishandles century-wide ranges. A rolling five-year
            # future horizon covers the published schedule without truncation.
            'end_date': f'{date.today().year + FUTURE_YEARS}-12-31',
            'per_page': 50,
            'page': page,
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    start = event.get('start_date') or ''
    venue_data = event.get('venue') or {}
    # This is ADDA's venue calendar. Some newly announced events omit the
    # room until later, but are still explicitly published as ADDA concerts.
    venue = clean_text(venue_data.get('venue')) or DEFAULT_VENUE

    try:
        start_datetime = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    if not title or not url:
        return None

    return {
        'title': title,
        'date': start_datetime.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start_datetime.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': 'ES',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)

    first_page = get_page(session)
    events = list(first_page.get('events') or [])
    total_pages = int(first_page.get('total_pages') or 1)

    # This WordPress host drops responses when pagination is requested too
    # aggressively, so keep concurrency deliberately low.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(get_page, session, page): page
            for page in range(2, total_pages + 1)
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                payload = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape event API page',
                    event='crawler_page_failed',
                    level='warning',
                    url=EVENTS_API,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            events.extend(payload.get('events') or [])

    records = [record for event in events if (record := parse_event(event))]
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class AddaalicanteEsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='addaalicante_es',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
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
        return get_concerts()


def main():
    AddaalicanteEsCrawler().run()


if __name__ == '__main__':
    main()
