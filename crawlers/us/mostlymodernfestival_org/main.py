from datetime import datetime
import html
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mostlymodernfestival.org/'
SOURCE = 'Mostly Modern Festival USA'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
FESTIVAL_CATEGORY_ID = 25

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
    text = html.unescape(str(value))
    if re.search(r'<[^>]+>', text):
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start_date(value):
    try:
        parsed = datetime.strptime(value or '', '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def event_to_record(event):
    title = clean_text(event.get('title'))
    event_date, time_from = parse_start_date(event.get('start_date'))
    url = clean_text(event.get('url'))

    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    if not all((title, event_date, url, venue, city)):
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_event_skipped',
            level='warning',
            url=url or API_URL,
            event_id=event.get('id'),
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
    while True:
        response = session.get(
            API_URL,
            params={
                'categories': FESTIVAL_CATEGORY_ID,
                'start_date': '2018-01-01',
                'end_date': '2100-12-31',
                'per_page': 50,
                'page': page,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()

        for event in payload.get('events', []):
            record = event_to_record(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No festival concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MostlyModernFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mostlymodernfestival_org',
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
    MostlyModernFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
