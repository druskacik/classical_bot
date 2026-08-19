import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://saltlakesymphony.org/'
EVENTS_URL = f'{SOURCE_URL}events/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Salt Lake Symphony'
CONCERT_CATEGORY_ID = 12

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': EVENTS_URL,
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True)
        if '<' in raw
        else raw.strip()
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start_date = clean_text(event.get('start_date'))
    venue_data = event.get('venue')
    venue_data = venue_data if isinstance(venue_data, dict) else {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    try:
        parsed_start = datetime.strptime(start_date, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None

    if not title or not url.startswith(('http://', 'https://')) or not venue or not city:
        return None

    time_from = None if event.get('all_day') else parsed_start.strftime('%H:%M')
    return {
        'title': title,
        'date': parsed_start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_period(start_year, end_year):
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(max_retries=Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(403, 429, 500, 502, 503, 504),
        )),
    )
    params = {
        'per_page': 50,
        'page': 1,
        'categories': CONCERT_CATEGORY_ID,
        'start_date': f'{start_year}-01-01T00:00:00',
        'end_date': f'{end_year}-12-31T23:59:59',
    }
    events = []
    while True:
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events.extend(payload.get('events', []))
        if params['page'] >= payload.get('total_pages', 1):
            break
        # The API's next_rest_url omits the category filter, so retain our
        # original parameters and advance the page explicitly.
        params['page'] += 1
    return events


class SaltLakeSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='saltlakesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        final_year = datetime.now().year + 10
        periods = [(1900, 1979)]
        periods.extend(
            (year, min(year + 4, final_year))
            for year in range(1980, final_year + 1, 5)
        )

        events = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(fetch_period, start_year, end_year): (start_year, end_year)
                for start_year, end_year in periods
            }
            for future in as_completed(futures):
                start_year, end_year = futures[future]
                try:
                    events.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Salt Lake Symphony concert period',
                        event='crawler_page_failed',
                        level='warning',
                        url=API_URL,
                        start_year=start_year,
                        end_year=end_year,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        skipped_count = 0
        for event in events:
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        if skipped_count:
            log_message(
                'Skipped incomplete Salt Lake Symphony concerts',
                event='crawler_items_skipped',
                level='warning',
                url=API_URL,
                record_count=skipped_count,
                error_type='IncompleteEventData',
                error_message='Required title, date, URL, venue, or city is missing',
            )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SaltLakeSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
