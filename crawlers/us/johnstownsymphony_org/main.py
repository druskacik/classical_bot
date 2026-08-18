import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.johnstownsymphony.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendarofevents')
SOURCE = 'Johnstown Symphony Orchestra'
TIME_ZONE = ZoneInfo('America/New_York')

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def calendar_pages(session):
    params = {'format': 'json'}
    seen_offsets = set()

    while True:
        response = session.get(CALENDAR_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        yield payload

        pagination = payload.get('pagination') or {}
        offset = pagination.get('nextPageOffset')
        if not pagination.get('nextPage') or not offset or offset in seen_offsets:
            break
        seen_offsets.add(offset)
        params = {'format': 'json', 'offset': offset}


def city_from_location(location):
    address = clean_text(location.get('addressLine2'))
    if address:
        # Squarespace stores this as "City, PA 15901" (with occasional
        # inconsistent commas and capitalization).
        city = re.split(r',\s*[A-Za-z]{2}(?:\s|,|$)', address, maxsplit=1)[0]
        city = city.strip(' ,')
        if city:
            return city

    venue = clean_text(location.get('addressTitle')).lower()
    known_cities = {
        'grand halle': 'Johnstown',
        'pasquerilla performing arts center': 'Johnstown',
        'grandview cemetery': 'Johnstown',
    }
    return next((city for name, city in known_cities.items() if name in venue), '')


def make_record(item):
    title = clean_text(item.get('title'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    path = item.get('fullUrl')
    start = item.get('startDate')

    if not title or not venue or not city or not path or not isinstance(start, (int, float)):
        return None

    try:
        starts_at = datetime.fromtimestamp(start / 1000, tz=TIME_ZONE)
    except (OverflowError, OSError, ValueError):
        return None

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(item.get('body') or item.get('excerpt')) or None,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    seen_ids = set()

    for payload in calendar_pages(session):
        for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
            item_id = item.get('id')
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            record = make_record(item)
            if record:
                records.append(record)

    log_message(
        'Calendar scrape completed',
        event='crawler_scrape_completed',
        record_count=len(records),
        candidate_count=len(seen_ids),
    )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class JohnstownSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='johnstownsymphony_org',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        try:
            return get_concerts()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape calendar',
                event='crawler_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise


def main():
    JohnstownSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
