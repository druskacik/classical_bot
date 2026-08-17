import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.baltimorephilharmonia.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Baltimore Philharmonia Orchestra'
TIME_ZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_PATTERN = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+(\d{1,2})\s*,?\s*(20\d{2})\b',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def html_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    return clean_text(soup.get_text('\n', strip=True))


def parse_date(value):
    match = DATE_PATTERN.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}',
            '%B %d %Y',
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    range_match = re.search(
        r'\b(1[0-2]|0?\d)(?::([0-5]\d))?\s*[-–]\s*'
        r'(?:1[0-2]|0?\d)(?::[0-5]\d)?\s*([ap])\.?m\.?\b',
        value,
        re.I,
    )
    if range_match:
        hour = int(range_match.group(1)) % 12
        if range_match.group(3).lower() == 'p':
            hour += 12
        return f'{hour:02d}:{int(range_match.group(2) or 0):02d}'
    match = re.search(r'\b(1[0-2]|0?\d)(?::([0-5]\d))?\s*([ap])\.?m\.?\b', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def archive_description(body):
    return html_text(body) or None


def archive_record(item):
    title = html_text(item.get('title'))
    full_url = clean_text(item.get('fullUrl'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    address_line = clean_text(location.get('addressLine2'))
    city = clean_text(address_line.split(',', 1)[0])
    try:
        start = datetime.fromtimestamp(int(item['startDate']) / 1000, TIME_ZONE)
        event_date = start.date().isoformat()
        time_from = start.strftime('%H:%M')
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        event_date = None
        time_from = None

    if not all((title, full_url, event_date, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': archive_description(item.get('body')),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def manual_event_groups(soup):
    for block in soup.select('.sqs-html-content'):
        paragraphs = [clean_text(node.get_text('\n', strip=True)) for node in block.select('p')]
        if sum(bool(DATE_PATTERN.search(text)) for text in paragraphs) < 2:
            continue
        if not any('Concert at' in text for text in paragraphs):
            continue

        current = []
        for text in paragraphs:
            if DATE_PATTERN.search(text):
                if current:
                    yield current
                current = [text]
            elif current and text:
                current.append(text)
        if current:
            yield current


def manual_record(parts):
    event_date = parse_date(parts[0])
    detail_lines = [line for part in parts[1:] for line in part.splitlines() if clean_text(line)]
    venue = None
    city = None
    time_from = None
    repertoire = []
    for line in detail_lines:
        line = clean_text(line)
        venue_match = re.match(r'Concert at\s+(.+)', line, re.I)
        if venue_match:
            venue = clean_text(venue_match.group(1))
            continue
        parsed_time = parse_time(line)
        if parsed_time:
            time_from = parsed_time
            continue
        address_match = re.search(r',\s*([^,]+),\s*Maryland\s+\d{5}\b', line, re.I)
        if address_match:
            city = clean_text(address_match.group(1))
            continue
        repertoire.append(line)

    title = repertoire[0] if repertoire else ''
    if not all((title, event_date, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': f'{CONCERTS_URL}#{event_date}',
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n'.join(repertoire) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BaltimorePhilharmoniaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='baltimorephilharmonia_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        page_response = session.get(CONCERTS_URL, timeout=45)
        page_response.raise_for_status()
        soup = BeautifulSoup(page_response.text, 'html.parser')

        api_response = session.get(CONCERTS_URL, params={'format': 'json'}, timeout=45)
        api_response.raise_for_status()
        payload = api_response.json()

        records = []
        for parts in manual_event_groups(soup):
            record = manual_record(parts)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Baltimore Philharmonia season entry',
                    event='crawler_item_skipped',
                    level='warning',
                    url=CONCERTS_URL,
                    error_type='IncompleteEventData',
                    error_message='Required date, title, venue, or city is missing',
                )

        for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
            record = archive_record(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Baltimore Philharmonia archive event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=urljoin(SOURCE_URL, clean_text(item.get('fullUrl'))),
                    error_type='IncompleteEventData',
                    error_message='Required date, title, URL, venue, or city is missing',
                )

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    BaltimorePhilharmoniaOrgCrawler().run()


if __name__ == '__main__':
    main()
