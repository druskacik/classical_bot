import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cisymphony.org/'
SOURCE = 'Central Iowa Symphony'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
DEFAULT_CITY = 'Ames'
DEFAULT_VENUE = 'Ames City Auditorium'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

SEASON_TITLE_RE = re.compile(r'^\s*(?:\d{2,4}-\d{2,4})\s+Season\s*$', re.I)
DATE_RE = re.compile(
    r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|'
    r'Dec(?:ember)?)\s+(\d{1,2}),\s+(\d{4})\b',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.I)
TIME_RANGE_RE = re.compile(
    r'\b(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*[-–—]\s*'
    r'\d{1,2}(?::\d{2})?\s*([ap])\.?m\.?',
    re.I,
)
KNOWN_VENUES = (
    'Ames City Auditorium',
    'Ames Bandshell Park',
    'Ames Golf & Country Club',
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month[:3]} {day} {year}', '%b %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    date_match = DATE_RE.search(value)
    if not date_match:
        return None
    match = TIME_RANGE_RE.search(value, date_match.end())
    if match:
        hour, minute, first_meridiem, end_meridiem = match.groups()
        meridiem = (first_meridiem or end_meridiem)[0]
    else:
        match = TIME_RE.search(value, date_match.end())
        if not match:
            return None
        hour, minute, meridiem = match.groups()
    hour = int(hour)
    if not 1 <= hour <= 12:
        return None
    hour = hour % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def parse_title(value):
    date_match = DATE_RE.search(value)
    if not date_match:
        return ''
    prefix = re.sub(r'[\s|,:;\-–—]+$', '', value[:date_match.start()]).strip()
    if 'cancel' not in prefix.lower():
        prefix = ''
    remainder = value[date_match.end():]
    remainder = TIME_RANGE_RE.sub('', remainder, count=1)
    remainder = TIME_RE.sub('', remainder, count=1)
    remainder = re.sub(r'^[\s|,:;\-–—]+', '', remainder)
    title = clean_text(remainder)
    return clean_text(f'{prefix} {title}') if prefix else title


def event_sections(html):
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.find_all(['h2', 'h3', 'h4'])
    for heading in headings:
        heading_text = clean_text(heading)
        if not DATE_RE.search(heading_text):
            continue

        parts = [heading_text]
        node = heading.find_next_sibling()
        while node and node.name not in {'h2', 'h3', 'h4'}:
            text = clean_text(node)
            if text:
                parts.append(text)
            node = node.find_next_sibling()
        yield heading_text, '\n\n'.join(parts)


def venue_from_text(value):
    for venue in KNOWN_VENUES:
        if venue.lower() in value.lower():
            return venue
    return DEFAULT_VENUE


def fetch_season_pages(session):
    response = session.get(
        API_URL,
        params={'per_page': 100, 'orderby': 'date', 'order': 'asc'},
        timeout=45,
    )
    response.raise_for_status()
    return [
        page for page in response.json()
        if SEASON_TITLE_RE.fullmatch(clean_text(page.get('title', {}).get('rendered')))
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    pages = fetch_season_pages(session)
    records = []

    for page in pages:
        page_url = page.get('link', '')
        html = page.get('content', {}).get('rendered', '')
        for heading, description in event_sections(html):
            event_date = parse_date(heading)
            title = parse_title(heading)
            venue = venue_from_text(description)
            if not event_date or not title or not page_url or not venue:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': page_url,
                'time_from': parse_time(heading),
                'venue': venue,
                'city': DEFAULT_CITY,
                'country_code': 'US',
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    records.sort(key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not records:
        log_message(
            'No season events found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return records


class CiSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cisymphony_org',
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
    CiSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
