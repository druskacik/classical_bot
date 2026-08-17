import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sfcmc.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'
SOURCE = 'San Francisco Community Music Center'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b'
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]m)\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    compact = re.sub(r'\s+', ' ', match.group(1).strip().upper())
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(compact, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def event_metadata(soup):
    label = soup.find(string=lambda text: text and text.strip() == 'Date / Time')
    if not label:
        return ''

    node = label.parent
    while node and node.name != 'body':
        text = clean_text(node.get_text('\n', strip=True))
        if 'Type of Event' in text and 'Location' in text:
            return text
        node = node.parent
    return ''


def parse_location(metadata):
    match = re.search(r'(?:^|\n)Location\n(.+?)(?=\nTickets(?:\n|$)|$)', metadata, re.S)
    if not match:
        return None, None

    lines = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    if not lines:
        return None, None
    venue = lines[0]
    address = ' '.join(lines[1:])

    city_match = re.search(r',\s*([A-Za-z .\'-]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b', address)
    if not city_match:
        city_match = re.search(r'\b(San Francisco|SF)\b', address, re.I)
    city = city_match.group(1).strip() if city_match else None
    if city and city.upper() == 'SF':
        city = 'San Francisco'
    return venue, city


def fetch_event(event, session=None):
    session = session or requests.Session()
    url = event.get('link', '')
    if not url:
        return None
    response = session.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    metadata = event_metadata(soup)
    event_date = parse_date(metadata)
    venue, city = parse_location(metadata)
    title = clean_text(event.get('title', {}).get('rendered'))
    if not title or not event_date or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(metadata),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(event.get('content', {}).get('rendered')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event_index(session):
    events = []
    page = 1
    while True:
        response = session.get(
            EVENTS_API_URL,
            params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'},
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        events.extend(batch)
        if page >= int(response.headers.get('X-WP-TotalPages', page)):
            return events
        page += 1


def scrape_concerts(session=None):
    session = session or requests.Session()
    events = fetch_event_index(session)
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_event, event): event for event in events}
        for future in as_completed(futures):
            event = futures[future]
            try:
                record = future.result()
            except requests.RequestException as error:
                log_message(
                    'Event detail request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=event.get('link'),
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
            url=EVENTS_API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SfcmcOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sfcmc_org',
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
    SfcmcOrgCrawler().run()


if __name__ == '__main__':
    main()
