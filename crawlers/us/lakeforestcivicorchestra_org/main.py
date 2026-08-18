import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lakeforestcivicorchestra.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-tickets/concert-tickets')
API_URL = f'{LISTING_URL}?format=json'
SOURCE = 'Lake Forest Civic Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'\b([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b')
TIME_LINE_RE = re.compile(r'(?i)\b(.+?)\s+concerts?\b')
TIME_RE = re.compile(r'(?i)\b(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))\b')
CITY_RE = re.compile(r'\bLake Forest\s*,\s*IL\b', re.IGNORECASE)


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
    normalized = re.sub(r'\.', '', value).upper().replace(' ', '')
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_times(description):
    for line in description.splitlines():
        concert_line = TIME_LINE_RE.search(line)
        if not concert_line:
            continue
        time_text = concert_line.group(1)
        shared_meridiem = re.search(
            r'(?i)\b(\d{1,2}(?::\d{2})?)\s*(?:&|and)\s*'
            r'(\d{1,2}(?::\d{2})?)\s*(a\.?m\.?|p\.?m\.?)\b',
            time_text,
        )
        if shared_meridiem:
            first, second, meridiem = shared_meridiem.groups()
            return [parse_time(f'{first}{meridiem}'), parse_time(f'{second}{meridiem}')]
        times = []
        for value in TIME_RE.findall(time_text):
            parsed = parse_time(value)
            if parsed and parsed not in times:
                times.append(parsed)
        return times
    return []


def parse_venue_and_city(description):
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    city = 'Lake Forest' if CITY_RE.search(description) else None
    venue = next((line for line in lines if line.lower() == 'gorton center'), None)
    return venue, city


def item_records(item):
    title = clean_text(item.get('title'))
    description = clean_text(item.get('excerpt') or item.get('body'))
    event_date = parse_date(description) or parse_date(title)
    full_url = item.get('fullUrl')
    url = urljoin(SOURCE_URL, full_url) if full_url else ''
    venue, city = parse_venue_and_city(description)

    # The category includes a season-subscription product. Requiring a concrete
    # date, venue, and city excludes that overview without title heuristics.
    if not all((title, event_date, url, venue, city)):
        return []

    times = parse_times(description) or [None]
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in times
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(API_URL, timeout=45)
    response.raise_for_status()
    payload = response.json()

    records = []
    for item in payload.get('items', []):
        records.extend(item_records(item))

    if not records:
        log_message(
            'No concrete concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda record: (
        record['date'], record['time_from'] or '', record['title'], record['url']
    ))


class LakeForestCivicOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lakeforestcivicorchestra_org',
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
    LakeForestCivicOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
