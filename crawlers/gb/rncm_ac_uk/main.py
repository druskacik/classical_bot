import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rncm.ac.uk/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/performances'
SITEMAP_URL = f'{SOURCE_URL}performance-sitemap.xml'
SOURCE = 'Royal Northern College of Music'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

VENUE_CITIES = {
    'BBC Philharmonic Studio (MediaCityUK)': 'Salford',
    "People's History Museum": 'Manchester',
    'The Bridgewater Hall': 'Manchester',
    'The Monastery Manchester': 'Manchester',
}

DATE_TIME_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    r'(\d{1,2})\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(20\d{2})(?:,\s*(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm))?',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_for_venue(venue):
    if venue.startswith('RNCM '):
        return 'Manchester'
    if venue in VENUE_CITIES:
        return VENUE_CITIES[venue]
    if re.search(r'\bManchester\b', venue, re.IGNORECASE):
        return 'Manchester'
    return None


def normalise_api_record(item):
    title = clean_text(BeautifulSoup(item.get('title', {}).get('rendered', ''), 'html.parser'))
    url = item.get('link')
    venue = clean_text(item.get('venue'))
    city = city_for_venue(venue)
    raw_datetime = item.get('performance_date_time')
    try:
        event_datetime = datetime.strptime(raw_datetime, '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return None

    if not title or not url or not venue or not city:
        return None

    content = item.get('production_content', {}).get('rendered', '')
    description = clean_text(BeautifulSoup(content, 'html.parser')) or None
    return {
        'title': title,
        'date': event_datetime.date().isoformat(),
        'url': url,
        'time_from': event_datetime.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_page(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('.event-title'))
    date_text = clean_text(soup.select_one('.event-subheading'))
    venue = clean_text(soup.select_one('.event-venue'))
    city = city_for_venue(venue)
    match = DATE_TIME_RE.match(date_text)
    if not title or not match or not venue or not city:
        return None

    day, month, year, hour, minute, meridiem = match.groups()
    try:
        event_date = datetime.strptime(f'{day} {month} {year}', '%d %B %Y').date().isoformat()
    except ValueError:
        return None

    time_from = None
    if hour and meridiem:
        hour_value = int(hour)
        if 1 <= hour_value <= 12:
            if meridiem.lower() == 'pm' and hour_value != 12:
                hour_value += 12
            elif meridiem.lower() == 'am' and hour_value == 12:
                hour_value = 0
            time_from = f'{hour_value:02d}:{int(minute or 0):02d}'

    description = clean_text(soup.select_one('.content-container')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_json(session, page):
    response = session.get(API_URL, params={'per_page': 100, 'page': page}, timeout=45)
    response.raise_for_status()
    return response


def get_records():
    session = requests.Session()
    session.headers.update(HEADERS)

    first_response = get_json(session, 1)
    total_pages = int(first_response.headers.get('X-WP-TotalPages', '1'))
    api_items = first_response.json()
    for page in range(2, total_pages + 1):
        api_items.extend(get_json(session, page).json())

    records = []
    api_urls = set()
    for item in api_items:
        record = normalise_api_record(item)
        if record:
            records.append(record)
            api_urls.add(record['url'].rstrip('/') + '/')

    sitemap_response = session.get(SITEMAP_URL, timeout=45)
    sitemap_response.raise_for_status()
    sitemap = BeautifulSoup(sitemap_response.content, 'xml')
    archived_urls = [
        clean_text(node)
        for node in sitemap.select('url > loc')
        if clean_text(node).rstrip('/') + '/' not in api_urls
    ]

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(session.get, url, timeout=45): url for url in archived_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                response = future.result()
                response.raise_for_status()
                record = parse_page(response.content, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape RNCM performance detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class RncmAcUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rncm_ac_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_records()


def main():
    RncmAcUkCrawler().run()


if __name__ == '__main__':
    main()
