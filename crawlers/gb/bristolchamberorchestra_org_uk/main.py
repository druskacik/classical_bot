import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bristolchamberorchestra.org.uk/'
SOURCE = 'Bristol Chamber Orchestra'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
CONCERTS_CATEGORY_ID = 12
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', html.unescape(str(value)).replace('\xa0', ' ')).strip()


def clean_description(value):
    if not value:
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for unwanted in soup.select('script, style, iframe, noscript'):
        unwanted.decompose()
    return clean_text(soup.get_text(' ', strip=True)) or None


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    start_value = event.get('start_date')
    venue_data = event.get('venue') if isinstance(event.get('venue'), dict) else {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))

    try:
        start = datetime.strptime(start_value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None

    if not all((title, url, venue, city)):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': None if event.get('all_day') else start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': clean_description(event.get('description')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BristolChamberOrchestraOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bristolchamberorchestra_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'categories': CONCERTS_CATEGORY_ID,
            'start_date': '1900-01-01 00:00:00',
            'end_date': '2100-12-31 23:59:59',
            'per_page': 50,
            'page': 1,
        }

        records = []
        while True:
            response = session.get(API_URL, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()

            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipping Bristol Chamber Orchestra event with incomplete required fields',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(event.get('url')),
                    )

            total_pages = int(payload.get('total_pages') or 1)
            if params['page'] >= total_pages:
                break
            params['page'] += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    BristolChamberOrchestraOrgUkCrawler().run()


if __name__ == '__main__':
    main()
