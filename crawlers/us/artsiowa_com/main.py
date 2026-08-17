import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestraiowa.org/performances-events/paramount-events/'
SOURCE = 'Orchestra Iowa - Paramount Events'
API_URL = 'https://orchestraiowa.org/wp-json/tribe/events/v1/events'
CATEGORY = 'paramount'

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
    text = unescape(str(value))
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    start_date = clean_text(event.get('start_date'))

    try:
        starts_at = datetime.strptime(start_date, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None

    if not title or not url.startswith(('http://', 'https://')) or not venue or not city:
        return None

    time_from = None if event.get('all_day') else starts_at.strftime('%H:%M')
    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        params = {
            'per_page': 50,
            'page': page,
            'start_date': '2000-01-01',
            'end_date': '2100-12-31',
            'categories': CATEGORY,
            'status': 'publish',
        }
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events') or []

        for event in events:
            categories = {item.get('slug') for item in event.get('categories') or []}
            if CATEGORY not in categories:
                continue
            record = parse_event(event)
            if record:
                records.append(record)

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not events:
            break
        page += 1

    if not records:
        log_message(
            'No Paramount events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ArtsIowaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='artsiowa_com',
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
        return scrape_events()


def main():
    ArtsIowaComCrawler().run()


if __name__ == '__main__':
    main()
