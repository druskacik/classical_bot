from datetime import datetime
from html import unescape
import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://auburnsymphony.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/concert'
SOURCE = 'Auburn Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Auburn School Park Preserve': 'Auburn',
    'Placer High School Theater': 'Auburn',
    'Mondavi Center for the Performing Arts': 'Davis',
}


def clean_text(value):
    if not value:
        return ''
    value = unescape(str(value))
    if '<' in value and '>' in value:
        value = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = value.replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value):
    try:
        parsed = datetime.strptime(clean_text(value), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_location(value):
    location = clean_text(value)
    venue = location.split(',', 1)[0].strip()
    city = VENUE_CITIES.get(venue)
    if not city:
        match = re.search(r'\b(Auburn|Davis)\b', location, re.IGNORECASE)
        city = match.group(1).title() if match else ''
    return venue, city


def build_description(item):
    acf = item.get('acf') or {}
    values = [
        acf.get('top_info_block'),
        acf.get('concert_info'),
        acf.get('custom_excerpt'),
        (item.get('excerpt') or {}).get('rendered'),
    ]
    parts = []
    for value in values:
        text = clean_text(value)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def record_from_item(item):
    acf = item.get('acf') or {}
    start = parse_start(acf.get('concert_start_date'))
    venue, city = parse_location(acf.get('location_address'))
    title = clean_text((item.get('title') or {}).get('rendered'))
    url = clean_text(item.get('link'))
    if not start or not title or not url or not venue or not city:
        log_message(
            'Skipping concert with incomplete required fields',
            event='crawler_record_skipped',
            level='warning',
            url=url or API_URL,
            record_id=item.get('id'),
        )
        return None

    event_date, time_from = start
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': build_description(item),
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
            params={'per_page': 100, 'page': page, 'orderby': 'date', 'order': 'asc'},
            timeout=45,
        )
        response.raise_for_status()
        items = response.json()
        for item in items:
            record = record_from_item(item)
            if record:
                records.append(record)

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No concerts found in API',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))


class AuburnSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='auburnsymphony_com',
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
    AuburnSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
