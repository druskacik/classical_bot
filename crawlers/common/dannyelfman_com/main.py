import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dannyelfman.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
COLLECTION_URL = urljoin(SOURCE_URL, 'events-posts?format=json')
SEATED_API_URL = (
    'https://cdn.seated.com/api/tour/'
    'fdd93f9b-0839-4643-befb-238afb257321?include=tour-events'
)
SOURCE = 'Danny Elfman'
SITE_TIMEZONE = ZoneInfo('America/Los_Angeles')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'australia': 'AU',
    'ca': 'US',  # Two legacy San Francisco records use the state as country.
    'canada': 'CA',
    'germany': 'DE',
    'italy': 'IT',
    'japan': 'JP',
    'mexico': 'MX',
    'spain': 'ES',
    'united kingdom': 'GB',
    'united states': 'US',
    'usa': 'US',
}

US_REGIONS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI',
    'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI',
    'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC',
    'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT',
    'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
}
CANADIAN_REGIONS = {
    'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC',
    'SK', 'YT',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


def collection_items(session):
    url = COLLECTION_URL
    seen_ids = set()
    items = []
    while url:
        payload = get_json(session, url)
        for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
            item_id = item.get('id')
            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                items.append(item)

        next_url = (payload.get('pagination') or {}).get('nextPageUrl')
        if next_url:
            separator = '&' if '?' in next_url else '?'
            url = urljoin(SOURCE_URL, f'{next_url}{separator}format=json')
        else:
            url = None
    return items


def collection_location(item):
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    address_line = clean_text(location.get('addressLine2'))
    city = address_line.split(',', 1)[0].strip()
    country = clean_text(location.get('addressCountry')).lower()
    country_code = COUNTRY_CODES.get(country)
    if not venue or not city or not country_code:
        return None
    return venue, city, country_code


def collection_record(item):
    title = clean_text(item.get('title'))
    location = collection_location(item)
    full_url = item.get('fullUrl')
    start_ms = item.get('startDate')
    if not title or not location or not full_url or not isinstance(start_ms, (int, float)):
        return None

    try:
        start = datetime.fromtimestamp(start_ms / 1000, tz=SITE_TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return None

    venue, city, country_code = location
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_text(item.get('body')) or clean_text(item.get('excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def tour_context(session):
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    paragraphs = []
    for paragraph in soup.find_all('p'):
        text = clean_text(paragraph)
        lowered = text.lower()
        if (
            'tour performances will showcase' in lowered
            or 'indoor concerts will be all rock' in lowered
            or 'outdoor/festival concerts' in lowered
            or 'band members include' in lowered
        ):
            if text not in paragraphs:
                paragraphs.append(text)
    return '\n\n'.join(paragraphs)


def seated_country(formatted_address):
    parts = [part.strip() for part in clean_text(formatted_address).split(',')]
    if len(parts) < 2:
        return None, None
    city = parts[0]
    region = re.sub(r'\s+\d.*$', '', parts[1]).strip().upper()
    if region in CANADIAN_REGIONS:
        return city, 'CA'
    if region in US_REGIONS:
        return city, 'US'
    return None, None


def seated_records(session):
    payload = get_json(session, SEATED_API_URL)
    context = tour_context(session)
    records = []
    for item in payload.get('included') or []:
        if item.get('type') != 'tour-events':
            continue
        attributes = item.get('attributes') or {}
        title = 'Danny Elfman – Fall Tour 2026'
        event_date = clean_text(attributes.get('starts-at-date-local'))
        venue = clean_text(attributes.get('venue-name'))
        city, country_code = seated_country(attributes.get('formatted-address'))
        item_id = item.get('id')
        try:
            event_date = datetime.strptime(event_date, '%Y-%m-%d').date().isoformat()
        except ValueError:
            continue
        if not item_id or not venue or not city or not country_code:
            continue

        description_parts = [clean_text(attributes.get('details')), context]
        description = '\n\n'.join(part for part in description_parts if part) or None
        records.append({
            'title': title,
            'date': event_date,
            'url': f'https://link.seated.com/{item_id}',
            # Seated exposes UTC in starts-at and no local clock field. Avoid
            # presenting UTC as the venue-local performance time.
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []

    for item in collection_items(session):
        record = collection_record(item)
        if record:
            records.append(record)

    try:
        records.extend(seated_records(session))
    except requests.RequestException as error:
        log_message(
            'Failed to scrape Seated tour feed',
            event='crawler_feed_failed',
            level='warning',
            url=SEATED_API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class DannyElfmanComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dannyelfman_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    DannyElfmanComCrawler().run()


if __name__ == '__main__':
    main()
