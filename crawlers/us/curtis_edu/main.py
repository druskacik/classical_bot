import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.curtis.edu/'
EVENTS_API_URL = urljoin(SOURCE_URL, 'wp-json/curtis/v1/events')
SOURCE = 'Curtis Institute of Music'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'austria': 'AT',
    'germany': 'DE',
    'greece': 'GR',
    'spain': 'ES',
    'united states': 'US',
    'usa': 'US',
}
CITY_COUNTRIES = {
    'athens': 'GR',
    'berlin': 'DE',
    'bremen': 'DE',
    'heiligendamm': 'DE',
    'madrid': 'ES',
    'salzburg': 'AT',
}
VENUE_LOCATIONS = {
    'die glocke, kleiner saal': ('Bremen', 'DE'),
    'französische friedrichstadtkirche': ('Berlin', 'DE'),
    'fundación juan march': ('Madrid', 'ES'),
    'grand hotel heiligendamm': ('Heiligendamm', 'DE'),
    'schloss leopoldskron': ('Salzburg', 'AT'),
}
US_STATE_RE = re.compile(r',\s*[A-Z]{2}(?:\b|$)')
TOUR_CITY_RE = re.compile(r'\b(?:in|to)\s+([^,|]+?)(?:,\s*([^|]+))?$', re.IGNORECASE)


def clean_text(value):
    if value is None:
        return ''
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_schema(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload.get('@graph', []) if isinstance(payload, dict) else []
        if isinstance(payload, dict):
            items = [payload, *items]
        elif isinstance(payload, list):
            items = payload
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def city_and_country(item, venue):
    title = clean_text(item.get('title'))
    categories = item.get('categories') or []
    if 'curtis-on-tour' not in categories:
        return 'Philadelphia', 'US'

    venue_location = VENUE_LOCATIONS.get(venue.lower())
    if venue_location:
        return venue_location

    match = TOUR_CITY_RE.search(title)
    if match:
        city = clean_text(match.group(1))
        suffix = clean_text(match.group(2))
        suffix_key = suffix.lower()
        if US_STATE_RE.search(', ' + suffix):
            return city, 'US'
        for name, code in COUNTRY_CODES.items():
            if name in suffix_key:
                return city, code
        code = CITY_COUNTRIES.get(city.lower())
        if code:
            return city, code

    searchable = f'{title} {venue}'.lower()
    for city, code in CITY_COUNTRIES.items():
        if city in searchable:
            return city.title(), code
    return None, None


def fetch_detail(session, item):
    url = urljoin(SOURCE_URL, clean_text(item.get('url')))
    response = session.get(url, timeout=45)
    response.raise_for_status()
    schema = event_schema(response.text)

    venue = clean_text(item.get('venue'))
    location = schema.get('location') if isinstance(schema, dict) else None
    if not venue and isinstance(location, dict):
        venue = clean_text(location.get('name'))
    city, country_code = city_and_country(item, venue)
    title = clean_text(item.get('title'))
    event_date = clean_text(item.get('date'))
    try:
        event_date = datetime.strptime(event_date, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None

    if not all((title, event_date, url, venue, city, country_code)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(item.get('time')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(schema.get('description')) or None,
    }


def scrape_events(session=None, max_workers=10):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_API_URL, timeout=60)
    response.raise_for_status()
    payload = response.json()
    items = payload.get('data', []) if payload.get('success') else []

    records = []
    skipped = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_detail, session, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = future.result()
            except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
                failed += 1
                log_message(
                    'Event detail request failed',
                    event='crawler_detail_failed',
                    level='warning',
                    url=urljoin(SOURCE_URL, clean_text(item.get('url'))),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                skipped += 1

    if skipped or failed:
        log_message(
            'Skipped events missing required details',
            event='crawler_records_skipped',
            level='warning',
            url=EVENTS_API_URL,
            record_count=skipped + failed,
        )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class CurtisEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='curtis_edu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_events()


def main():
    CurtisEduCrawler().run()


if __name__ == '__main__':
    main()
