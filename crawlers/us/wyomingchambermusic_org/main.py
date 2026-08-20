import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wyomingchambermusic.org/'
SOURCE = 'Wyoming International Chamber Music Festival'
EVENTS_URL = urljoin(SOURCE_URL, 'events-2-1')
SITE_TIME_ZONE = ZoneInfo('Europe/Zurich')
DEFAULT_VENUE = "St. Matthew's Cathedral"
DEFAULT_CITY = 'Laramie'

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
    for element in soup(['style', 'script']):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def parse_city(location):
    address = ' '.join(
        value for value in (location.get('addressLine1', ''), location.get('addressLine2', ''))
        if value
    )
    known_city = re.search(
        r'\b(Laramie|Cheyenne)\s*,\s*(?:WY|Wyoming)\b', address, re.IGNORECASE
    )
    if known_city:
        return known_city.group(1).title()
    match = re.search(r'(?:^|,)\s*([^,]+),\s*(?:WY|Wyoming)\b', address, re.IGNORECASE)
    return match.group(1).strip() if match else None


def parse_location(item):
    location = item.get('location') or {}
    venue = unescape(location.get('addressTitle') or '').strip()
    city = parse_city(location)
    if venue and city:
        return venue, city

    combined = ' '.join(
        str(value or '')
        for value in (
            item.get('title'),
            clean_html(item.get('body')),
            location.get('addressLine1'),
            location.get('addressLine2'),
        )
    )
    if "st. matthew" in combined.lower() and 'laramie' in combined.lower():
        return DEFAULT_VENUE, DEFAULT_CITY

    # The festival states that St. Matthew's Cathedral in Laramie is its host.
    # Entries elsewhere name their touring venue, so this is a defensible fallback
    # only when the event supplies no location at all.
    if not any((venue, location.get('addressLine1'), location.get('addressLine2'))):
        return DEFAULT_VENUE, DEFAULT_CITY
    return None


def parse_item(item):
    title = (item.get('title') or '').strip()
    full_url = item.get('fullUrl') or ''
    start_timestamp = item.get('startDate')
    location = parse_location(item)
    if not title or not full_url or not isinstance(start_timestamp, (int, float)) or not location:
        return None

    start = datetime.fromtimestamp(start_timestamp / 1000, tz=SITE_TIME_ZONE)
    venue, city = location
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_html(item.get('body')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class WyomingChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wyomingchambermusic_org',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = []
        offset = 0

        while True:
            params = {'format': 'json'}
            if offset:
                params['offset'] = offset
            try:
                response = session.get(
                    EVENTS_URL,
                    params=params,
                    timeout=45,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Wyoming chamber music events',
                    event='crawler_fetch_failed',
                    level='error',
                    url=EVENTS_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            page_items = list(payload.get('upcoming') or []) + list(payload.get('past') or [])
            if not page_items:
                break
            items.extend(page_items)

            collection = payload.get('collection') or {}
            page_size = collection.get('pageSize') or len(page_items)
            item_count = collection.get('itemCount')
            offset += page_size
            if isinstance(item_count, int) and offset >= item_count:
                break

        records = [record for item in items if (record := parse_item(item))]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    WyomingChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
