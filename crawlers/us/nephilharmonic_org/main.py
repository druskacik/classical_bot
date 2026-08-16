import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nephilharmonic.org/'
SOURCE = 'New England Philharmonic'
COLLECTION_URL = urljoin(SOURCE_URL, 'concerts')
TIME_ZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9',
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'boston university tsai performance center': 'Boston',
    'tsai performance center': 'Boston',
    'willett hall of united parish brookline': 'Brookline',
    "united parish of brookline's willett hall": 'Brookline',
    'first church cambridge': 'Cambridge',
    'first church, cambridge': 'Cambridge',
    'first church in cambridge': 'Cambridge',
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup(['script', 'style']):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_location(location):
    address_line = html.unescape(location.get('addressLine2') or '')
    city = address_line.split(',', 1)[0].strip()
    return city or None


def city_for_venue(venue):
    normalized = re.sub(r'[^a-z0-9]+', ' ', venue.lower()).strip()
    for name, city in VENUE_CITIES.items():
        if re.sub(r'[^a-z0-9]+', ' ', name).strip() in normalized:
            return city
    return None


def homepage_venue_hints(home_html, titles):
    """Recover venues omitted from an event's Squarespace location fields."""
    text = clean_html(home_html)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    hints = {}
    for title in titles:
        try:
            start = next(i for i, line in enumerate(lines) if line == title)
        except StopIteration:
            continue
        nearby = lines[start + 1:start + 8]
        for index, line in enumerate(nearby[:-1]):
            if re.search(r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b', line):
                venue = nearby[index + 1]
                city = city_for_venue(venue)
                if city:
                    hints[title] = (venue, city)
                break
    return hints


def parse_item(item, venue_hints):
    title = clean_html(item.get('title'))
    full_url = item.get('fullUrl')
    start_timestamp = item.get('startDate')
    if not title or not full_url or not isinstance(start_timestamp, (int, float)):
        return None

    start = datetime.fromtimestamp(start_timestamp / 1000, tz=TIME_ZONE)
    location = item.get('location') or {}
    venue = clean_html(location.get('addressTitle'))
    city = city_from_location(location)
    if (not venue or not city) and title in venue_hints:
        venue, city = venue_hints[title]
    if not venue or not city:
        return None

    body = clean_html(item.get('body'))
    excerpt = clean_html(item.get('excerpt'))
    description_parts = []
    for part in (excerpt, body):
        if part and part not in description_parts:
            description_parts.append(part)

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class NephilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nephilharmonic_org',
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
        offset = None

        try:
            while True:
                params = {'format': 'json'}
                if offset is not None:
                    params['offset'] = str(offset)
                response = session.get(COLLECTION_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
                items.extend(payload.get('upcoming') or [])
                items.extend(payload.get('past') or [])
                pagination = payload.get('pagination') or {}
                if not pagination.get('nextPage'):
                    break
                next_offset = pagination.get('nextPageOffset')
                if next_offset is None or next_offset == offset:
                    raise ValueError('Squarespace pagination did not provide a new offset')
                offset = next_offset

            home_response = session.get(SOURCE_URL, timeout=45)
            home_response.raise_for_status()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch New England Philharmonic events',
                event='crawler_fetch_failed',
                level='error',
                url=COLLECTION_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        unique_items = {}
        for item in items:
            key = item.get('id') or (item.get('fullUrl'), item.get('startDate'))
            unique_items[key] = item

        hints = homepage_venue_hints(
            home_response.text,
            [clean_html(item.get('title')) for item in unique_items.values()],
        )
        records = []
        for item in unique_items.values():
            record = parse_item(item, hints)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    NephilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
