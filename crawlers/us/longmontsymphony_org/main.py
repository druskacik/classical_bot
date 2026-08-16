import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://longmontsymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'season-concerts')
SOURCE = 'Longmont Symphony Orchestra'
# Squarespace stores this calendar in the site's configured America/Chicago
# timezone; using it reproduces the times displayed on the first-party pages.
LOCAL_TIMEZONE = ZoneInfo('America/Chicago')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    return re.sub(r'\s+', ' ', html.unescape(str(value or '')).replace('\xa0', ' ')).strip()


def body_text(value):
    soup = BeautifulSoup(value or '', 'html.parser')
    for unwanted in soup.select('script, style'):
        unwanted.decompose()
    return clean_text(soup.get_text(' ', strip=True)) or None


def local_datetime(timestamp):
    if not isinstance(timestamp, (int, float)):
        return None
    return datetime.fromtimestamp(timestamp / 1000, tz=LOCAL_TIMEZONE)


def city_from_location(location):
    address_line = clean_text(location.get('addressLine2'))
    if address_line:
        city = clean_text(address_line.split(',', 1)[0])
        if city:
            return city
    return None


def fallback_location(title, description):
    evidence = clean_text(f'{title} {description}')
    if re.search(r'Annual Home Tour|Historical West Side homes', evidence, re.I):
        return 'Longmont Historic West Side homes', 'Longmont'
    if re.search(r'July 4th Concert|concert at Thompson Park', evidence, re.I):
        return 'Thompson Park', 'Longmont'
    if re.search(r'private home in Lafayette', evidence, re.I):
        return 'Private home', 'Lafayette'
    return None, None


def parse_event(item):
    title = clean_text(item.get('title'))
    start = local_datetime(item.get('startDate'))
    full_url = clean_text(item.get('fullUrl'))
    description = body_text(item.get('body'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle')) or None
    city = city_from_location(location)

    if not venue or not city:
        fallback_venue, fallback_city = fallback_location(title, description)
        venue = venue or fallback_venue
        city = city or fallback_city

    if not title or not start or not full_url or not venue or not city:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LongmontsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='longmontsymphony_org',
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
        try:
            response = requests.get(
                EVENTS_URL,
                params={'format': 'json'},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch concert calendar',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
        records = []
        for item in items:
            record = parse_event(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event with incomplete required fields',
                    event='crawler_item_skipped',
                    level='warning',
                    url=urljoin(SOURCE_URL, clean_text(item.get('fullUrl'))),
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    LongmontsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
