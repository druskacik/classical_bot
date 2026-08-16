from datetime import datetime
import html
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://greatlakeschambermusic.org/'
SOURCE = 'Great Lakes Chamber Music Festival'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'Canada': 'CA',
    'United States': 'US',
    'United States of America': 'US',
    'USA': 'US',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    country_name = clean_text(venue_data.get('country'))
    country_code = COUNTRY_CODES.get(country_name)

    try:
        start = datetime.strptime(event.get('start_date', ''), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    if not all((title, url, venue, city, country_code)):
        return None

    description_parts = [
        clean_text(event.get('description')),
        clean_text(event.get('excerpt')),
    ]
    description = '\n\n'.join(dict.fromkeys(part for part in description_parts if part)) or None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    params = {
        'start_date': '2000-01-01',
        'end_date': '2100-12-31',
        'per_page': 50,
        'page': 1,
        'status': 'publish',
    }

    records = []
    skipped_count = 0
    while True:
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for event in payload.get('events', []):
            record = parse_event(event)
            if record:
                records.append(record)
            else:
                skipped_count += 1

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1

    if skipped_count:
        log_message(
            'Skipped events missing required date or location data',
            event='crawler_records_skipped',
            level='warning',
            url=API_URL,
            record_count=skipped_count,
        )
    if not records:
        log_message(
            'No valid events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue'], item['url']): item
        for item in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class GreatLakesChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='greatlakeschambermusic_org',
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
        return scrape_concerts()


def main():
    GreatLakesChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
