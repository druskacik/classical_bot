import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.music.uga.edu/'
LISTING_URL = urljoin(SOURCE_URL, 'events/all')
SOURCE = 'Hugh Hodgson School of Music'
DEFAULT_CITY = 'Athens'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_location(value):
    location = clean_text(value)
    if not location:
        return '', ''

    city = DEFAULT_CITY
    city_match = re.search(r'\b([^,|]+),\s*GA(?:\s+\d{5})?\b', location, re.I)
    if city_match:
        city = clean_text(city_match.group(1)).split('|')[-1].strip()

    # Some locations append a street address after a pipe. Keep only the named
    # performance space; addresses and ticketing prose do not belong in venue.
    venue = location.split('|', 1)[0].strip()
    venue = re.sub(r'\s*\b\d{1,6}\s+[^,]+,\s*GA(?:\s+\d{5})?\b.*$', '', venue, flags=re.I)
    return venue.strip(' ,;-'), city


def parse_listing_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    events = []
    for row in soup.select('.views-row'):
        link = row.select_one('a[href*="/events/content/"]')
        time_node = row.select_one('.views-field-field-date-time-1 time[datetime]')
        location_node = row.select_one('.views-field-field-location .field-content')
        if not link or not time_node or not location_node:
            continue

        title_node = link.select_one('.wrap_clear_left')
        title = clean_text(title_node)
        event_date, time_from = parse_datetime(time_node.get('datetime'))
        venue, city = parse_location(location_node)
        url = urljoin(page_url, link.get('href', ''))
        if not all((title, event_date, url, venue, city)):
            continue
        events.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
        })

    next_link = soup.select_one('li.pager__item--next a[href], a[rel="next"][href]')
    next_url = urljoin(page_url, next_link.get('href')) if next_link else None
    return events, next_url


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    body = soup.select_one('article .body')
    if not body:
        return None

    for unwanted in body.select('script, style, noscript'):
        unwanted.decompose()
    description = clean_text(body)
    return description or None


def fetch_description(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_description(response.text)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    events = []
    seen_pages = set()
    next_url = LISTING_URL
    while next_url and next_url not in seen_pages:
        seen_pages.add(next_url)
        response = session.get(next_url, timeout=45)
        response.raise_for_status()
        page_events, next_url = parse_listing_page(response.text, response.url)
        events.extend(page_events)

    descriptions = {}
    unique_urls = sorted({event['url'] for event in events})
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_description, url): url for url in unique_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Unable to fetch event detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                descriptions[url] = None

    records = []
    seen_records = set()
    for event in events:
        key = (event['url'], event['date'], event['time_from'])
        if key in seen_records:
            continue
        seen_records.add(key)
        records.append({
            **event,
            'country_code': 'US',
            'description': descriptions.get(event['url']),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No events found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class MusicUgaEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='music_uga_edu',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MusicUgaEduCrawler().run()


if __name__ == '__main__':
    main()
