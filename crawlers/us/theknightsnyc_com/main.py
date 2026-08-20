import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.theknightsnyc.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'performances-all')
SOURCE = 'The Knights'
SITE_TIME_ZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

COUNTRY_CODES = {
    'canada': 'CA',
    'denmark': 'DK',
    'germany': 'DE',
    'united states': 'US',
    'usa': 'US',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(unescape(str(value)), 'html.parser')
    for element in soup(['script', 'style']):
        element.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def city_from_location(location):
    address_line = clean_text(location.get('addressLine2'))
    if not address_line:
        return None
    # Squarespace locations use either "City, region, postal code" or
    # "City postal-code". Region and postal-code text is not a city.
    city = address_line.split(',', 1)[0].strip()
    city = re.sub(r'\s+\d{4,6}\s*$', '', city).strip()
    return city or None


def country_from_location(location):
    country = clean_text(location.get('addressCountry')).lower()
    if country in COUNTRY_CODES:
        return COUNTRY_CODES[country]
    if country:
        return None

    # Every blank-country location currently exposed by this US ensemble is
    # in the United States and carries a US state abbreviation or a known NYC
    # borough in addressLine2. Do not extend this fallback to touring records.
    address_line = clean_text(location.get('addressLine2'))
    if re.search(r'(?:,|\s)\s*[A-Z]{2}(?:\s*,?\s*\d{5})?\s*$', address_line):
        return 'US'
    if address_line.strip() in {'Brooklyn', 'New York'}:
        return 'US'
    return None


def local_datetime(milliseconds):
    try:
        # Squarespace stores calendar wall times relative to the website time
        # zone, including for tour dates; this reproduces the displayed date.
        return datetime.fromtimestamp(float(milliseconds) / 1000, SITE_TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None


def record_from_item(item):
    categories = item.get('categories') or []
    if 'Broadcasts' in categories:
        return None

    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    country_code = country_from_location(location)
    start = local_datetime(item.get('startDate'))
    if not all((title, path, venue, city, country_code, start)):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(item.get('body')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_page(session, offset=None):
    params = {'format': 'json'}
    if offset is not None:
        params['offset'] = offset
    response = session.get(EVENTS_URL, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    seen_offsets = set()
    offset = None

    while offset not in seen_offsets:
        seen_offsets.add(offset)
        try:
            payload = fetch_page(session, offset)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch The Knights performance calendar',
                event='crawler_page_failed',
                level='warning',
                url=EVENTS_URL,
                offset=offset,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            if not records:
                raise
            break

        for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
            record = record_from_item(item)
            if record:
                records.append(record)

        pagination = payload.get('pagination') or {}
        if not pagination.get('nextPage'):
            break
        offset = pagination.get('nextPageOffset')
        if offset is None:
            break

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue'], item['url']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))
    if not result:
        log_message(
            'No valid The Knights performances found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return result


class TheKnightsNycComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='theknightsnyc_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    TheKnightsNycComCrawler().run()


if __name__ == '__main__':
    main()
