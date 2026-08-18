import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import local
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brightmusic.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Brightmusic Chamber Ensemble'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_FORMATS = (
    '%A, %B %d, %Y - %I:%M%p',
    '%A, %B %d, %Y - %I%p',
)

KNOWN_VENUE_CITIES = {
    'First Baptist Church': 'Oklahoma City',
    'First Baptist Church of Oklahoma City': 'Oklahoma City',
    "Saint Paul's Cathedral": 'Oklahoma City',
    "St. Paul's Cathedral": 'Oklahoma City',
    'All Souls Episcopal Church': 'Oklahoma City',
    "All Souls' Episcopal Church": 'Oklahoma City',
    'First Christian Church of Norman': 'Norman',
    'First Presbyterian Church of Norman': 'Norman',
    'Norman School for Strings': 'Norman',
}

_thread_state = local()


def session_for_thread():
    session = getattr(_thread_state, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _thread_state.session = session
    return session


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    normalized = re.sub(r'\s+', ' ', clean_text(value)).replace('a.m.', 'am').replace('p.m.', 'pm')
    for pattern in DATE_FORMATS:
        try:
            parsed = datetime.strptime(normalized, pattern)
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            pass
    return None


def city_from_venue(venue, location_url=None):
    for name, city in KNOWN_VENUE_CITIES.items():
        if name.lower() in venue.lower():
            return city

    if not location_url:
        return None
    try:
        response = session_for_thread().get(location_url, timeout=45)
        response.raise_for_status()
        text = clean_text(BeautifulSoup(response.text, 'html.parser').select_one('#content'))
    except requests.RequestException as error:
        log_message(
            'Venue page request failed',
            event='crawler_venue_request_failed',
            level='warning',
            url=location_url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    for city in ('Oklahoma City', 'Norman', 'Edmond', 'Bartlesville'):
        if re.search(rf'\b{re.escape(city)}\b', text, re.I):
            return city
    return None


def detail_records(url):
    try:
        response = session_for_thread().get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_request_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    node = soup.select_one('.node')
    title_node = soup.select_one('h2.content-title')
    if not node or not title_node:
        return []

    title = clean_text(title_node)
    date_nodes = node.select('[class*="field-name-field-concert-date-"]')
    location_node = node.select_one('[class*="field-name-field-location-"]')
    venue = clean_text(location_node)
    location_link = location_node.select_one('a[href]') if location_node else None
    location_url = urljoin(url, location_link['href']) if location_link else None
    city = city_from_venue(venue, location_url)
    if not title or not venue or not city:
        log_message(
            'Skipping concert with incomplete core fields',
            event='crawler_record_skipped',
            level='warning',
            url=url,
            venue=venue or None,
            has_title=bool(title),
            has_venue=bool(venue),
            has_city=bool(city),
        )
        return []

    description_nodes = node.select(
        '.field-name-field-header, .field-name-body, .field-name-field-program-notes'
    )
    description_parts = []
    for description_node in description_nodes:
        text = clean_text(description_node)
        text = re.sub(r'^(?:Program Notes?|Program):\s*', '', text, flags=re.I)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None

    records = []
    for date_node in date_nodes:
        parsed = parse_datetime(date_node)
        if not parsed:
            continue
        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts():
    response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    urls = sorted({
        urljoin(CONCERTS_URL, link['href'])
        for link in soup.select('.view-concerts a[href*="/concert/"]')
    })

    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detail_records, url): url for url in urls}
        for future in as_completed(futures):
            try:
                records.extend(future.result())
            except Exception as error:
                log_message(
                    'Unexpected concert parsing failure',
                    event='crawler_detail_parse_failed',
                    level='warning',
                    url=futures[future],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    unique_records = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique_records.setdefault(key, record)
    return sorted(
        unique_records.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class BrightmusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brightmusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BrightmusicOrgCrawler().run()


if __name__ == '__main__':
    main()
