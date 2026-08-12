import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://stmagnusfestival.com/'
SOURCE = 'St Magnus International Festival'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
ARCHIVE_RE = re.compile(r'/festival-programme-(20\d{2})/?$')
EVENT_RE = re.compile(r'/festival-programme-20\d{2}/[^/]+/?$')
DATE_TIME_RE = re.compile(
    r'\b([A-Z][a-z]+ \d{1,2}, 20\d{2})\s+at\s+'
    r'(\d{1,2}):([0-5]\d)\s*(am|pm)\b',
    re.IGNORECASE,
)
POSTCODE_RE = re.compile(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', re.IGNORECASE)

# Some venue addresses omit their town. These venue names provide stronger
# evidence than applying a blanket Kirkwall default to events across Orkney.
VENUE_CITIES = {
    'St Magnus Cathedral': 'Kirkwall',
    'St Magnus Centre': 'Kirkwall',
    'Orkney Auction Mart': 'Kirkwall',
    'Stromness Academy Theatre': 'Stromness',
    'Stromness Town Hall': 'Stromness',
    'Hoy Kirk': 'Hoy',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(url, attempts=4):
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers=HEADERS, timeout=45)
            if response.status_code == 429 and attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise last_error


def archive_urls():
    soup = BeautifulSoup(get_response(SOURCE_URL).content, 'html.parser')
    urls = {
        urljoin(SOURCE_URL, anchor.get('href'))
        for anchor in soup.select('a[href]')
        if ARCHIVE_RE.search(urlparse(urljoin(SOURCE_URL, anchor.get('href'))).path)
    }
    return sorted(urls)


def event_urls(archives):
    urls = set()
    for archive_url in archives:
        soup = BeautifulSoup(get_response(archive_url).content, 'html.parser')
        for anchor in soup.select('a[href]'):
            url = urljoin(archive_url, anchor.get('href'))
            if EVENT_RE.search(urlparse(url).path):
                urls.add(url)
    return sorted(urls)


def parse_date_time(value):
    match = DATE_TIME_RE.search(value)
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None, None
    hour = int(match.group(2))
    if not 1 <= hour <= 12:
        return None, None
    if match.group(4).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(4).lower() == 'am' and hour == 12:
        hour = 0
    return event_date, f'{hour:02d}:{match.group(3)}'


def parse_venue_city(value):
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if not parts:
        return None, None
    venue = parts[0]
    for known_venue, city in VENUE_CITIES.items():
        if known_venue.lower() in venue.lower():
            return venue, city

    locality_parts = [POSTCODE_RE.sub('', part).strip(' .') for part in parts[1:]]
    locality_parts = [part for part in locality_parts if part and not re.search(r'\d', part)]
    return (venue, locality_parts[-1]) if locality_parts else (None, None)


def description_text(section):
    parts = []
    for node in section.select('.lhs .artists, .lhs .additional-description, :scope > .copy'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    section = soup.select_one('section.event-info')
    if not section:
        return None
    title = clean_text(section.select_one('h2.page-title'))
    title = re.sub(r'^\d+\s*-\s*', '', title).strip()
    event_date, time_from = parse_date_time(clean_text(section.select_one('.event-time')))
    venue, city = parse_venue_city(clean_text(section.select_one('.venue-address')))
    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description_text(section),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    urls = event_urls(archive_urls())
    records = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(get_response, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_event(future.result().content, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape St Magnus Festival event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class StMagnusFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stmagnusfestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    StMagnusFestivalComCrawler().run()


if __name__ == '__main__':
    main()
