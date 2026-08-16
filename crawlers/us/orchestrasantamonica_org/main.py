import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestrasantamonica.org/'
SOURCE = 'Orchestra Santa Monica'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

# The season page explicitly says that every 2026-27 concert is at this hall.
# This covers the one API event whose venue relation is currently missing.
SEASON_VENUE_DEFAULTS = {
    '2026-2027-season': ('Eli and Edythe Broad Stage', 'Santa Monica'),
}


def clean_text(value):
    if not value:
        return ''
    value = str(value)
    if '<' not in value:
        return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()
    soup = BeautifulSoup(value, 'html.parser')
    for node in soup(['script', 'style', 'noscript']):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def clean_title(value):
    title = clean_text(html.unescape(value or ''))
    title = re.sub(r'\s*\((?:[A-Z]{3,9}\s+)?\d{4}\)\s*$', '', title)
    return re.sub(
        r'\s*[-–—]\s*(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|'
        r'Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|'
        r'Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\s*$',
        '',
        title,
        flags=re.IGNORECASE,
    ).strip()


def parse_datetime(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


def venue_data(event):
    venue = event.get('venue')
    if isinstance(venue, dict):
        name = clean_text(venue.get('venue'))
        city = clean_text(venue.get('city'))
        if name and city:
            return name, city

    category_slugs = {
        category.get('slug')
        for category in event.get('categories', [])
        if isinstance(category, dict)
    }
    for slug, default in SEASON_VENUE_DEFAULTS.items():
        if slug in category_slugs:
            return default
    return '', ''


def event_to_record(event):
    start = parse_datetime(event.get('start_date'))
    title = clean_title(event.get('title'))
    url = clean_text(event.get('url'))
    venue, city = venue_data(event)
    if not start or not title or not url.startswith(('http://', 'https://')) or not venue or not city:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 50,
                'page': page,
                'start_date': '1900-01-01',
                'end_date': '2100-12-31',
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events', [])
        for event in events:
            record = event_to_record(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped event with incomplete required fields',
                    event='crawler_event_skipped',
                    level='warning',
                    url=event.get('url'),
                    event_id=event.get('id'),
                )

        total_pages = int(payload.get('total_pages') or 1)
        if page >= total_pages or not events:
            break
        page += 1

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class OrchestraSantaMonicaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestrasantamonica_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
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
        return scrape_concerts()


def main():
    OrchestraSantaMonicaOrgCrawler().run()


if __name__ == '__main__':
    main()
