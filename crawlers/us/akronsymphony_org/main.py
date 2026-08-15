import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://akronsymphony.org/'
SOURCE = 'Akron Symphony Orchestra'
EVENTS_API = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
CONCERT_CATEGORY_ID = 25
PAGE_SIZE = 50

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, page):
    response = session.get(
        EVENTS_API,
        params={
            'categories': CONCERT_CATEGORY_ID,
            'start_date': '2000-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
            'per_page': PAGE_SIZE,
            'page': page,
        },
        timeout=45,
    )
    response.raise_for_status()
    return response.json()


def make_record(event):
    title = clean_text(event.get('title'))
    url = event.get('url') or ''
    start = event.get('start_date') or ''
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2}', start)
    if not title or not url or not match or not venue or not city:
        return None

    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    time_from = None if event.get('all_day') else f'{match.group(2)}:{match.group(3)}'
    description = clean_text(event.get('description')) or clean_text(event.get('excerpt')) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        payload = get_page(session, page)
        events = payload.get('events') or []
        for event in events:
            record = make_record(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=event.get('url'),
                )

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not events:
            break
        page += 1

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class AkronSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='akronsymphony_org',
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
        return get_concerts()


def main():
    AkronSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
