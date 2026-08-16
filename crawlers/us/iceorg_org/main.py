import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://iceorg.org/'
CALENDAR_URL = f'{SOURCE_URL}events'
SOURCE = 'International Contemporary Ensemble'
TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'australia': 'AU',
    'austria': 'AT',
    'belgium': 'BE',
    'canada': 'CA',
    'ecuador': 'EC',
    'finland': 'FI',
    'france': 'FR',
    'germany': 'DE',
    'netherlands': 'NL',
    'scotland': 'GB',
    'switzerland': 'CH',
    'united kingdom': 'GB',
    'united states': 'US',
    'usa': 'US',
}

US_STATES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI',
    'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI',
    'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC',
    'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT',
    'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
}


def clean_text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', html.unescape(str(value))).strip()


def clean_description(value):
    if not isinstance(value, str) or not value.strip():
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup.select('script, style, noscript'):
        element.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or None


def parse_city(location):
    address_line2 = clean_text(location.get('addressLine2'))
    if address_line2:
        city = address_line2.split(',', 1)[0].strip()
        if city and city.lower() not in {'california'}:
            return city

    combined = ' '.join(
        clean_text(location.get(key))
        for key in ('addressTitle', 'addressLine1', 'addressCountry')
    )
    if re.search(r'\b(?:New York|NYC)\b', combined, re.I):
        return 'New York'
    return None


def parse_country_code(location):
    country = clean_text(location.get('addressCountry')).lower()
    if country in COUNTRY_CODES:
        return COUNTRY_CODES[country]
    if country in {'new york', 'new jersey'} or re.fullmatch(r'(?:ny\s*)?\d{5}', country):
        return 'US'

    address = ' '.join(
        clean_text(location.get(key))
        for key in ('addressLine1', 'addressLine2', 'addressCountry')
    )
    state_match = re.search(r',\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\b', address)
    if state_match and state_match.group(1) in US_STATES:
        return 'US'
    return None


def parse_event(item):
    if not isinstance(item, dict):
        return None

    title = clean_text(item.get('title'))
    full_url = clean_text(item.get('fullUrl'))
    timestamp = item.get('startDate')
    location = item.get('location') if isinstance(item.get('location'), dict) else {}
    venue = clean_text(location.get('addressTitle'))
    city = parse_city(location)
    country_code = parse_country_code(location)

    if not isinstance(timestamp, (int, float)):
        return None
    try:
        start = datetime.fromtimestamp(timestamp / 1000, tz=TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return None

    if not all((title, full_url, venue, city, country_code)):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': f'{SOURCE_URL.rstrip("/")}/{full_url.lstrip("/")}',
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': clean_description(item.get('body')) or clean_description(item.get('excerpt')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class IceorgOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='iceorg_org',
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
        records = []
        seen_ids = set()
        offset = None

        while True:
            params = {'format': 'json'}
            if offset is not None:
                params['offset'] = offset
            try:
                response = requests.get(
                    CALENDAR_URL, params=params, headers=HEADERS, timeout=45
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch International Contemporary Ensemble calendar page',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if 'response' in locals() else CALENDAR_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
                item_id = clean_text(item.get('id'))
                if item_id and item_id in seen_ids:
                    continue
                record = parse_event(item)
                if record:
                    records.append(record)
                elif isinstance(item, dict):
                    log_message(
                        'Skipped incomplete International Contemporary Ensemble event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=f'{SOURCE_URL.rstrip("/")}/{clean_text(item.get("fullUrl")).lstrip("/")}',
                        error_type='IncompleteEventData',
                        error_message='Required date, title, URL, venue, city, or country is missing',
                    )
                if item_id:
                    seen_ids.add(item_id)

            pagination = payload.get('pagination') or {}
            next_offset = pagination.get('nextPageOffset') if pagination.get('nextPage') else None
            if next_offset is None or next_offset == offset:
                break
            offset = next_offset

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    IceorgOrgCrawler().run()


if __name__ == '__main__':
    main()
