import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.dso.org/'
LISTING_URL = urljoin(SOURCE_URL, 'events-and-tickets/list')
SOURCE = 'Detroit Symphony Orchestra'

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
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_iso_datetime(value):
    if not value:
        return None
    normalized = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value)
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def occurrence_dates(start, end):
    """Expand short consecutive runs, but reject season/program date spans."""
    if not start:
        return []
    start_date = start.date()
    end_date = end.date() if end else start_date
    day_count = (end_date - start_date).days
    if day_count < 0:
        return []
    if day_count > 4:
        return []
    return [start_date + timedelta(days=offset) for offset in range(day_count + 1)]


def event_urls(listing_soup):
    urls = []
    seen = set()
    for card in listing_soup.select('a.event-card[href]'):
        href = card.get('href', '')
        title = clean_text(card.select_one('.event-card__title'))
        if not title or '/events-and-tickets/events/' not in href:
            continue
        url = urljoin(SOURCE_URL, href)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    schema_node = soup.find('script', type='application/ld+json')
    if not schema_node or not schema_node.string:
        return []
    try:
        schema = json.loads(schema_node.string)
    except json.JSONDecodeError:
        return []
    if schema.get('@type') != 'Event':
        return []

    title = clean_text(schema.get('name'))
    location = schema.get('location') or {}
    venue = clean_text(location.get('name'))
    address = location.get('address') or {}
    city = clean_text(address.get('addressLocality'))
    start = parse_iso_datetime(schema.get('startDate'))
    end = parse_iso_datetime(schema.get('endDate'))
    dates = occurrence_dates(start, end)
    if not title or not venue or not city or not dates:
        return []

    description_parts = []
    description_node = soup.select_one('.event-details__description')
    if description_node:
        description_parts.append(clean_text(description_node))
    for piece in soup.select('.program__piece'):
        composer = clean_text(piece.select_one('.program__artist'))
        work = clean_text(piece.select_one('.program__title'))
        program_line = ' — '.join(part for part in (composer, work) if part)
        if program_line:
            description_parts.append(program_line)
    description = '\n\n'.join(part for part in description_parts if part) or None

    records = []
    for event_date in dates:
        records.append({
            'title': title,
            'date': event_date.isoformat(),
            'url': url,
            # The page exposes only the first and last timestamps, not the
            # start time of every performance in a multi-date production.
            'time_from': start.strftime('%H:%M') if len(dates) == 1 else None,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    urls = event_urls(BeautifulSoup(response.text, 'html.parser'))

    def fetch_event(url):
        error = None
        detail_session = requests.Session()
        detail_session.headers.update(HEADERS)
        for attempt in range(3):
            try:
                detail = detail_session.get(url, timeout=45)
                detail.raise_for_status()
                return parse_event_page(detail.text, url)
            except requests.RequestException as caught:
                error = caught
                if attempt < 2:
                    time.sleep(1 + attempt)
        log_message(
            'Failed to fetch DSO event page',
            event='crawler_detail_fetch_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    records = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch_event, url): url for url in urls}
        for future in as_completed(futures):
            records.extend(future.result())

    if not records:
        log_message(
            'No DSO event occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class DsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dso_org',
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
    DsoOrgCrawler().run()


if __name__ == '__main__':
    main()
