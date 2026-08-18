import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.helenasymphony.org/'
SOURCE = 'Helena Symphony'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value))
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None, None
    return parsed.date().isoformat(), parsed.time().strftime('%H:%M')


def event_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    date, time_from = parse_start(event.get('start_date'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    # This organization publishes its own Helena season. Some venue objects omit
    # their city, but the event archive and venue pages consistently place them
    # in Helena, Montana.
    city = clean_text(venue_data.get('city')) or 'Helena'

    if not all((title, date, url, venue, city)):
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url or API_URL,
            event_id=event.get('id'),
        )
        return None

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    params = {
        # Supplying a start date makes the API include its retained archive;
        # leaving the end open also keeps future seasons discoverable.
        'start_date': '2000-01-01',
        'per_page': 50,
        'page': 1,
        'status': 'publish',
    }
    records = []

    while True:
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for event in payload.get('events', []):
            record = event_record(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1

    if not records:
        log_message(
            'No usable events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class HelenaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='helenasymphony_org',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    HelenaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
