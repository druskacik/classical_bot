import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://leapevents.com/'
SOURCE = 'Leap Event Discovery'
SEARCH_URL = 'https://leapevents.com/wp-json/tcc/v1/events/search'
CATEGORY = 'Music & Concerts'

# A 5,000 km circle around the geographic centre covers the contiguous US.
# Separate centres cover Alaska, Hawaii, and Puerto Rico without relying on the
# visitor geolocation which the discovery page normally sends to the API.
SEARCH_CENTRES = [
    (39.8283, -98.5795, 5000),
    (64.2008, -152.4937, 1800),
    (20.7984, -156.3319, 800),
    (18.2208, -66.5901, 500),
]
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': SOURCE_URL,
}
MAX_PAGES = 250


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    text = html.unescape(soup.get_text('\n', strip=True)).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def search_documents(session):
    documents = {}
    for lat, lon, radius_km in SEARCH_CENTRES:
        for page in range(1, MAX_PAGES + 1):
            response = session.get(
                SEARCH_URL,
                params={
                    'format': 'json',
                    'size': 100,
                    'page': page,
                    'category': CATEGORY,
                    'lat': lat,
                    'lon': lon,
                    'radius_km': radius_km,
                },
                timeout=45,
            )
            response.raise_for_status()
            page_documents = response.json().get('documents') or []
            if not page_documents:
                break
            for document in page_documents:
                url = document.get('event_url')
                if url:
                    documents[url] = document
        else:
            log_message(
                'Search pagination reached safety limit',
                event='crawler_pagination_limit',
                level='warning',
                url=SEARCH_URL,
                page=MAX_PAGES,
            )
    return list(documents.values())


def event_schema(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return None


def parse_start(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def recurring_occurrences(session, url, page_text, fallback_start):
    match = re.search(r'var\s+dates_avail\s*=\s*(\{.*?\})\s*;', page_text, re.S)
    if not match:
        return [fallback_start] if fallback_start else []
    try:
        dates = list(json.loads(match.group(1)))
    except json.JSONDecodeError:
        return [fallback_start] if fallback_start else []

    occurrences = []
    endpoint = f'{url.rstrip("/")}/recurring-event-times'
    for date in dates:
        try:
            response = session.get(endpoint, params={'date': date}, timeout=30)
            response.raise_for_status()
            times = response.json().get('times') or []
        except (requests.RequestException, ValueError):
            times = []
        for item in times:
            time_match = re.search(r'(\d{1,2}):(\d{2})\s*([AP]M)', item.get('time', ''), re.I)
            if not time_match:
                continue
            hour = int(time_match.group(1)) % 12
            if time_match.group(3).upper() == 'PM':
                hour += 12
            occurrences.append(datetime.fromisoformat(f'{date}T{hour:02d}:{time_match.group(2)}'))
        if not times:
            occurrences.append(datetime.fromisoformat(date))
    return occurrences


def document_to_records(document):
    url = document.get('event_url')
    if not url:
        return []
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Could not fetch event detail',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return []
    location = schema.get('location') or {}
    address = location.get('address') or {}
    country = clean_text(address.get('addressCountry')).upper()
    title = clean_text(schema.get('name'))
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    canonical_url = schema.get('url') or response.url
    if country not in {'US', 'USA', 'UNITED STATES'} or not title or not venue or not city:
        return []

    fallback_start = parse_start(schema.get('startDate'))
    occurrences = recurring_occurrences(session, canonical_url, response.text, fallback_start)
    description = clean_text(schema.get('description')) or None
    records = []
    for starts_at in occurrences:
        if not starts_at:
            continue
        records.append({
            'title': title,
            'date': starts_at.date().isoformat(),
            'url': canonical_url,
            'time_from': starts_at.strftime('%H:%M') if starts_at.hour or starts_at.minute else None,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    documents = search_documents(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(document_to_records, document) for document in documents]
        for future in as_completed(futures):
            records.extend(future.result())
    if not records:
        log_message(
            'No US music events found',
            event='crawler_empty_listing',
            level='warning',
            url=SEARCH_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ShowclixComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='showclix_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ShowclixComCrawler().run()


if __name__ == '__main__':
    main()
