from datetime import datetime
from html import unescape
import re
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bridgechambermusicfestival.com/'
SOURCE = 'Bridge Chamber Music Festival'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
API_URL = f'{EVENTS_URL}?format=json'
TIME_ZONE = ZoneInfo('America/Chicago')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_html(value):
    if not value:
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for unwanted in soup.select('style, script'):
        unwanted.decompose()
    text = '\n'.join(
        line.strip() for line in soup.get_text('\n', strip=True).splitlines() if line.strip()
    )
    return text or None


def parse_city(location, body):
    directions = BeautifulSoup(body or '', 'html.parser').select_one('a[href*="google.com/maps"]')
    if directions:
        coordinates = re.search(r'@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)', directions['href'])
        if coordinates:
            latitude, longitude = map(float, coordinates.groups())
            if 44.40 <= latitude <= 44.52 and -93.25 <= longitude <= -93.05:
                return 'Northfield'
    latitude = location.get('markerLat')
    longitude = location.get('markerLng')
    if (
        isinstance(latitude, (int, float))
        and isinstance(longitude, (int, float))
        and 44.40 <= latitude <= 44.52
        and -93.25 <= longitude <= -93.05
    ):
        return 'Northfield'
    address = location.get('addressLine2') or ''
    city = address.split(',', 1)[0].strip()
    return city or 'Northfield'


def parse_event(item):
    title = (item.get('title') or '').strip()
    path = item.get('fullUrl') or ''
    location = item.get('location') or {}
    venue = unescape(location.get('addressTitle') or '').strip()
    timestamp = item.get('startDate')
    if not title or not path or not venue or not isinstance(timestamp, (int, float)):
        return None

    try:
        start = datetime.fromtimestamp(timestamp / 1000, tz=TIME_ZONE)
    except (OverflowError, OSError, ValueError):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': parse_city(location, item.get('body')),
        'country_code': 'US',
        'description': clean_html(item.get('body')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BridgeChamberMusicFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bridgechambermusicfestival_com',
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
        try:
            response = requests.get(API_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Bridge Chamber Music Festival events',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
        records = [record for item in items if (record := parse_event(item))]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    BridgeChamberMusicFestivalComCrawler().run()


if __name__ == '__main__':
    main()
