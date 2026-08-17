import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wheelingsymphony.com/'
SITEMAP_URL = f'{SOURCE_URL}event-sitemap.xml'
SOURCE = 'Wheeling Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_FORMATS = ('%A, %B %d, %Y', '%B %d, %Y')
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]m)\b', re.IGNORECASE)
CITY_STATE_RE = re.compile(
    r'(?:^|,)\s*([^,]+?)\s*,\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?\s*$',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value):
    value = clean_text(value)
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    return ''


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    normalized = re.sub(r'\s+', ' ', match.group(1)).upper()
    for time_format in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, time_format).strftime('%H:%M')
        except ValueError:
            pass
    return None


def extract_city(address):
    match = CITY_STATE_RE.search(clean_text(address))
    return clean_text(match.group(1)) if match else ''


def sidebar_text(soup, heading):
    for node in soup.select('.sidebar-group'):
        title = node.find(['h2', 'h3', 'h4', 'h5'])
        if clean_text(title).lower() == heading.lower():
            title.extract()
            return clean_text(node)
    return ''


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.event')
    if not article:
        return None

    title = clean_text(article.select_one('.post-header__heading'))
    event_date = parse_date(article.select_one('.post-header__date'))
    meta_items = article.select('.post-header__meta-item')
    venue = clean_text(meta_items[0]) if meta_items else ''
    time_from = parse_time(meta_items[1]) if len(meta_items) > 1 else None

    location = sidebar_text(soup, 'Location')
    city = extract_city(location) or extract_city(venue)
    if extract_city(venue):
        venue = re.sub(r',\s*[^,]+,\s*[A-Z]{2}\s*$', '', venue, flags=re.IGNORECASE).strip()
    if not city and re.search(r'\bWheeling\b', location, re.IGNORECASE):
        city = 'Wheeling'
    if not city and not location:
        # Venue-only entries in this local orchestra calendar are Wheeling events;
        # touring entries identify their destination in the venue or address fields.
        city = 'Wheeling'

    description_parts = []
    details = sidebar_text(soup, 'Details')
    body = article.select_one('.entry-content__content')
    for value in (details, clean_text(body)):
        if value and value not in description_parts:
            description_parts.append(value)

    if not all((title, event_date, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def sitemap_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return sorted({clean_text(node) for node in soup.find_all('loc') if '/events/' in clean_text(node)})


def fetch_event(url):
    for attempt in range(3):
        response = requests.get(url, headers=HEADERS, timeout=45)
        if response.status_code == 404:
            # The site's sitemap retains some event URLs after their pages are removed.
            return None
        if response.status_code not in {429, 500, 502, 503, 504}:
            response.raise_for_status()
            return parse_event_page(response.text, url)
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    response.raise_for_status()


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = sitemap_urls(session)
    records = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_event, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Event page request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    if not records:
        log_message(
            'No parseable events found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class WheelingSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wheelingsymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    WheelingSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
