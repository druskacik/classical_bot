import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sost.org/'
SOURCE = 'Symphony of Southeast Texas'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

API_PARAMS = {
    'per_page': 50,
    'start_date': '1900-01-01 00:00:00',
    'end_date': '2100-12-31 23:59:59',
    # Parent category 23 includes all of the site's concert-series children.
    'categories': 23,
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    for node in soup.select('script, style, img, noscript'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start_date(event):
    value = event.get('start_date')
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None

    time_from = None if event.get('all_day') else parsed.strftime('%H:%M')
    return parsed.date().isoformat(), time_from


def event_to_record(event):
    title = clean_text(event.get('title'))
    url = event.get('url')
    event_date, time_from = parse_start_date(event)

    venue_data = event.get('venue')
    if not isinstance(venue_data, dict):
        venue_data = {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    if not all((title, event_date, url, venue, city)):
        log_message(
            'Skipping event with missing required fields',
            event='crawler_event_skipped',
            level='warning',
            url=url or API_URL,
            event_id=event.get('id'),
            missing_date=not bool(event_date),
            missing_venue=not bool(venue),
            missing_city=not bool(city),
        )
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    records = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        response = session.get(
            API_URL,
            params={**API_PARAMS, 'page': page},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()

        events = payload.get('events')
        if not isinstance(events, list):
            raise ValueError('SOST events API returned an invalid events payload')

        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)

        try:
            total_pages = max(1, int(payload.get('total_pages', 1)))
        except (TypeError, ValueError):
            raise ValueError('SOST events API returned invalid pagination metadata')
        page += 1

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SostOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sost_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    SostOrgCrawler().run()


if __name__ == '__main__':
    main()
