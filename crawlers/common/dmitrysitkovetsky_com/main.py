import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dmitrysitkovetsky.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Dmitry Sitkovetsky'
SITE_TIMEZONE = ZoneInfo('Europe/London')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'Austria': 'AT',
    'Azerbaijan': 'AZ',
    'Belgium': 'BE',
    'Bulgaria': 'BG',
    'China': 'CN',
    'Czechia': 'CZ',
    'France': 'FR',
    'Germany': 'DE',
    'Hungary': 'HU',
    'Italy': 'IT',
    'Japan': 'JP',
    'Latvia': 'LV',
    'Lithuania': 'LT',
    'Malta': 'MT',
    'Poland': 'PL',
    'Romania': 'RO',
    'Slovenia': 'SI',
    'United Kingdom': 'GB',
    'United States': 'US',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_location(location):
    line = clean_text(location.get('addressLine2'))
    if not line:
        return None
    city = line.split(',', 1)[0].strip()
    city = re.sub(r'^\d{4,6}(?:-\d{3})?\s+', '', city).strip()
    if not city or city.lower() in {'chateau', 'castle'}:
        return None
    return city


def parse_item(item):
    title = clean_text(item.get('title'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    country_name = clean_text(location.get('addressCountry')).title()
    country_code = COUNTRY_CODES.get(country_name)
    start_timestamp = item.get('startDate')

    # Squarespace sometimes stores the event title in the venue field. That is
    # not a defensible venue, so omit the occurrence rather than inventing one.
    if not title or not venue or venue.casefold() == title.casefold():
        return None
    if not city or not country_code or not isinstance(start_timestamp, (int, float)):
        return None

    try:
        start = datetime.fromtimestamp(start_timestamp / 1000, SITE_TIMEZONE)
    except (OverflowError, OSError, ValueError):
        return None

    description = clean_text(item.get('body') or item.get('excerpt')) or None
    full_url = clean_text(item.get('fullUrl'))
    if not full_url:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class DmitrySitkovetskyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dmitrysitkovetsky_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        offset = None
        seen_offsets = set()

        while True:
            params = {'format': 'json'}
            if offset is not None:
                params['offset'] = offset
            response = None
            try:
                response = session.get(CALENDAR_URL, params=params, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Dmitry Sitkovetsky calendar',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if response is not None else CALENDAR_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
                record = parse_item(item)
                if record:
                    records.append(record)

            pagination = payload.get('pagination') or {}
            next_offset = pagination.get('nextPageOffset')
            if (
                not pagination.get('nextPage')
                or next_offset is None
                or next_offset in seen_offsets
            ):
                break
            seen_offsets.add(next_offset)
            offset = next_offset

        unique = {
            (record['title'], record['date'], record['time_from'], record['venue']): record
            for record in records
        }
        return sorted(
            unique.values(),
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    DmitrySitkovetskyComCrawler().run()


if __name__ == '__main__':
    main()
