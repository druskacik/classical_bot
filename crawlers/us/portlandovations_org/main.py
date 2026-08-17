import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://portlandovations.org/'
ARCHIVE_URL = f'{SOURCE_URL}event/'
SOURCE = 'Portland Ovations'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

PERFORMANCE_RE = re.compile(
    r'^(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),\s+'
    r'([A-Z]+\s+\d{1,2},\s+\d{4})\s+[–-]\s+'
    r'(\d{1,2}(?::\d{2})?\s*(?:AM|PM))$',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def archive_event_urls():
    first_soup = get_soup(ARCHIVE_URL)
    page_numbers = []
    for node in first_soup.select('a.page-numbers[href]'):
        match = re.search(r'/page/(\d+)/', node.get('href', ''))
        if match:
            page_numbers.append(int(match.group(1)))
    last_page = max(page_numbers, default=1)
    soups = [first_soup]
    if last_page > 1:
        with ThreadPoolExecutor(max_workers=8) as executor:
            soups.extend(executor.map(
                get_soup,
                [f'{ARCHIVE_URL}page/{page}/' for page in range(2, last_page + 1)],
            ))

    urls = set()
    for soup in soups:
        for link in soup.select('h2.entry-title a[href]'):
            url = urljoin(ARCHIVE_URL, link.get('href'))
            if '/event/' in url:
                urls.add(url)
    return sorted(urls)


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value.get('@graph', []) if isinstance(value, dict) else []
        candidates = [value, *candidates] if isinstance(value, dict) else candidates
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return {}


def parse_datetime_line(line):
    match = PERFORMANCE_RE.match(clean_text(line))
    if not match:
        return None
    try:
        value = datetime.strptime(
            f'{match.group(1)} {match.group(2).upper()}', '%B %d, %Y %I:%M %p'
        )
    except ValueError:
        try:
            value = datetime.strptime(
                f'{match.group(1)} {match.group(2).upper()}', '%B %d, %Y %I %p'
            )
        except ValueError:
            return None
    return value.date().isoformat(), value.strftime('%H:%M')


def schema_occurrences(schema):
    start_value = schema.get('startDate')
    end_value = schema.get('endDate') or start_value
    if not start_value:
        return []
    try:
        start = datetime.fromisoformat(start_value.replace('Z', '+00:00'))
        end = datetime.fromisoformat(end_value.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return []

    # JSON-LD ranges on this site represent a performance on every date.
    occurrences = []
    current = start.date()
    while current <= end.date():
        occurrences.append((current.isoformat(), start.strftime('%H:%M') if start.hour else None))
        current += timedelta(days=1)
    return occurrences


def parse_event(url):
    soup = get_soup(url)
    schema = event_schema(soup)
    title_node = soup.select_one('.event-banner h1, h1.entry-title')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else schema.get('name'))
    venue_node = soup.select_one('.event-banner .venue')
    location = schema.get('location') or {}
    venue = clean_text(
        venue_node.get_text(' ', strip=True) if venue_node else location.get('name')
    )

    main_node = soup.select_one('main')
    main_text = clean_text(main_node.get_text('\n', strip=True) if main_node else '')
    occurrences = []
    for line in main_text.splitlines():
        parsed = parse_datetime_line(line)
        if parsed and parsed not in occurrences:
            occurrences.append(parsed)
    if not occurrences:
        occurrences = schema_occurrences(schema)

    city = ''
    if venue:
        venue_city = re.search(
            rf'^{re.escape(venue)}\s*,\s*([^\n]+)$', main_text, re.IGNORECASE | re.MULTILINE
        )
        if venue_city:
            city = clean_text(venue_city.group(1)).title()
        elif 'westbrook' in venue.lower():
            city = 'Westbrook'

    content_node = soup.select_one('.entry-content')
    description = clean_text(content_node.get_text('\n', strip=True) if content_node else '')
    description = re.split(r'\nPERFORMANCE TIMES?\n', description, maxsplit=1, flags=re.I)[0]
    description = description or None

    if not title or not venue or not city or not occurrences:
        return []

    return [{
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
    } for event_date, time_from in occurrences]


def scrape_concerts():
    urls = archive_event_urls()
    records = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(parse_event, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Event request failed',
                    event='crawler_event_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    if not records:
        log_message(
            'No valid events found',
            event='crawler_empty_listing',
            level='warning',
            url=ARCHIVE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PortlandOvationsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='portlandovations_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PortlandOvationsOrgCrawler().run()


if __name__ == '__main__':
    main()
