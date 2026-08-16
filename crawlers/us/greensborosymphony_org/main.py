import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://greensborosymphony.org/'
SOURCE = 'Greensboro Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

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
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start = clean_text(event.get('start_date'))
    venue_data = event.get('venue') or {}
    if isinstance(venue_data, list):
        venue_data = venue_data[0] if venue_data else {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    if not title or not url or not start or not venue or not city:
        return None
    try:
        event_date, event_time = start.split(' ', 1)
        date.fromisoformat(event_date)
    except (TypeError, ValueError):
        return None

    description = clean_text(
        BeautifulSoup(event.get('description') or '', 'html.parser')
    ) or None
    time_from = None if event.get('all_day') else event_time[:5]
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


class GreensboroSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='greensborosymphony_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            response = session.get(
                API_URL,
                params={
                    'page': page,
                    'per_page': 50,
                    'start_date': '2000-01-01',
                    'end_date': '2100-12-31',
                    'status': 'publish',
                },
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()

            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Greensboro Symphony event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(event.get('url')),
                        error_type='IncompleteEventData',
                        error_message='Required date, title, URL, venue, or city is missing',
                    )

            if page >= int(payload.get('total_pages') or 1):
                break
            page += 1

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    GreensboroSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
