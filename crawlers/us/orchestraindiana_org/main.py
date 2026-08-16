import html
import json
import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestraindiana.org/'
SOURCE = 'Orchestra Indiana'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/ajde_events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# EventON only exposes a street address in its schema. These first-party venue
# names and addresses identify their Indiana cities unambiguously.
VENUE_CITIES = {
    'Emens Auditorium': 'Muncie',
    'Euler Science Complex at Taylor University': 'Upland',
    'Muncie Central High School': 'Muncie',
    'Pruis Hall': 'Muncie',
    'Rediger Chapel/Auditorium at Taylor University': 'Upland',
    'Sursa Performance Hall': 'Muncie',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n')
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def get_published_events(session):
    events = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, 'status': 'publish'},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError('Orchestra Indiana event API returned a non-list payload')
        events.extend(payload)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return events
        page += 1


def event_schema(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
        except (TypeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') in {'Event', 'MusicEvent'}:
                return candidate
    return None


def parse_datetime(value):
    match = re.match(
        r'^(\d{4})-(\d{1,2})-(\d{1,2})T(\d{1,2}):(\d{2})',
        str(value or ''),
    )
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups()))
    except ValueError:
        return None


def location_fields(payload):
    location = payload.get('location') or {}
    if isinstance(location, list):
        location = next((item for item in location if isinstance(item, dict)), {})
    if not isinstance(location, dict):
        return '', ''

    venue = clean_text(location.get('name'))
    address = location.get('address') or {}
    if not isinstance(address, dict):
        address = {}
    city = clean_text(address.get('addressLocality'))
    if not city:
        street = clean_text(address.get('streetAddress'))
        address_match = re.search(r',\s*([^,]+),\s*IN\s+\d{5}(?:-\d{4})?\b', street, re.I)
        city = clean_text(address_match.group(1)) if address_match else ''
    return venue, city or VENUE_CITIES.get(venue, '')


def record_from_detail(page_html, fallback_url):
    payload = event_schema(page_html)
    if not payload:
        return None

    start = parse_datetime(payload.get('startDate'))
    title = clean_text(payload.get('name'))
    url = str(payload.get('url') or fallback_url).strip()
    venue, city = location_fields(payload)
    parsed_url = urlparse(url)
    if not start or not title or not venue or not city or parsed_url.scheme not in {'http', 'https'}:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(payload.get('description')) or None,
    }


class OrchestraindianaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestraindiana_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = make_session()
        try:
            try:
                events = get_published_events(session)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Orchestra Indiana event API',
                    event='crawler_listing_request_failed',
                    level='error',
                    url=API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            records = []
            for event in events:
                url = str(event.get('link') or '').strip()
                if not url:
                    continue
                try:
                    response = session.get(url, timeout=45)
                    response.raise_for_status()
                    record = record_from_detail(response.text, url)
                    if record:
                        records.append(record)
                    else:
                        log_message(
                            'Skipping event without a usable occurrence, venue, or city',
                            event='crawler_event_skipped',
                            level='warning',
                            url=url,
                        )
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Orchestra Indiana event detail',
                        event='crawler_detail_request_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

            if not records:
                log_message(
                    'No usable Orchestra Indiana events found',
                    event='crawler_empty_listing',
                    level='warning',
                    url=API_URL,
                    record_count=0,
                )
            return sorted(
                records,
                key=lambda item: (item['date'], item['time_from'] or '', item['title']),
            )
        finally:
            session.close()


def main():
    OrchestraindianaOrgCrawler().run()


if __name__ == '__main__':
    main()
