import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bpo.org/'
SOURCE = 'Buffalo Philharmonic Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(value), 'html.parser')
    text = soup.get_text(' ', strip=True)
    text = re.sub(r'\[/?(?:vc_[^\]]+|vc_row|vc_column)[^\]]*\]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def parse_event(event):
    details = event.get('start_date_details') or {}
    try:
        event_date = date(
            int(details['year']), int(details['month']), int(details['day'])
        ).isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    venue_data = event.get('venue') or {}
    title = clean_html(event.get('title'))
    venue = clean_html(venue_data.get('venue'))
    city = clean_html(venue_data.get('city'))
    url = event.get('url')
    if not all((title, event_date, url, venue, city)):
        return None

    time_from = None
    if not event.get('all_day'):
        try:
            time_from = f"{int(details['hour']):02d}:{int(details['minutes']):02d}"
        except (KeyError, TypeError, ValueError):
            time_from = None

    description = clean_html(event.get('description')) or clean_html(event.get('excerpt'))
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BpoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bpo_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1

        while True:
            try:
                response = session.get(
                    API_URL,
                    params={
                        'per_page': 50,
                        'page': page,
                        'start_date': '2000-01-01 00:00:00',
                        'end_date': '2100-12-31 23:59:59',
                        'status': 'publish',
                    },
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch BPO events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            events = payload.get('events') or []
            for event in events:
                record = parse_event(event)
                if record:
                    records.append(record)

            total_pages = int(payload.get('total_pages') or 1)
            if page >= total_pages:
                break
            page += 1

        return records


def main():
    return BpoOrgCrawler().run()


if __name__ == '__main__':
    main()
