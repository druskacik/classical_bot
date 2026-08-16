import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://owensborosymphony.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Owensboro Symphony'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalBot/1.0)',
    'Accept': 'application/json',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def venue_details(event):
    venue = event.get('venue') or {}
    venue_name = clean_text(venue.get('venue'))
    city = clean_text(venue.get('city'))

    # The Stroll is deliberately spread across several downtown stations, so
    # The Events Calendar has no single venue object for these occurrences.
    if not venue_name and 'symphony stroll' in clean_text(event.get('title')).lower():
        venue_name = 'Downtown Owensboro Riverfront'
        city = city or 'Owensboro'

    return venue_name, city


def event_to_record(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = parse_datetime(event.get('start_date'))
    venue, city = venue_details(event)

    if not all((title, url, start, venue, city)):
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url or EVENTS_API_URL,
            event_id=event.get('id'),
        )
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
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
            EVENTS_API_URL,
            params={
                'start_date': '2000-01-01',
                'end_date': '2100-12-31',
                'status': 'publish',
                'per_page': 50,
                'page': page,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []

        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No concerts found in events API',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class OwensboroSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='owensborosymphony_org',
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
    OwensboroSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
