import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bargemusic.org/'
SOURCE = 'Bargemusic'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
ARCHIVE_START = '2000-01-01 00:00:00'
PER_PAGE = 50

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
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
    # Old event descriptions contain visible Divi shortcodes.
    text = re.sub(r'\[/?et_pb_[^\]]+\]', '', text)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start_date = clean_text(event.get('start_date'))
    try:
        starts_at = datetime.strptime(start_date, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None

    # Bargemusic moved from its barge at Fulton Ferry Landing to the Brooklyn
    # Bridge Park Boathouse for the April 2025 season. API venue objects lack
    # venue names and cities, so use the institution's documented locations.
    venue = (
        'Brooklyn Bridge Park Boathouse'
        if starts_at.date().isoformat() >= '2025-04-05'
        else 'Bargemusic'
    )
    if not all((title, url)):
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': url,
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': 'Brooklyn',
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BargemusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bargemusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        records = []
        page = 1

        while True:
            response = session.get(
                EVENTS_API_URL,
                params={
                    'start_date': ARCHIVE_START,
                    'per_page': PER_PAGE,
                    'page': page,
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            events = payload.get('events') or []

            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Bargemusic event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(event.get('url')),
                        error_type='IncompleteEventData',
                        error_message='Required title, URL, or valid start date is missing',
                    )

            total_pages = int(payload.get('total_pages') or 1)
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    BargemusicOrgCrawler().run()


if __name__ == '__main__':
    main()
