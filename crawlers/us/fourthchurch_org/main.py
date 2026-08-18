import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fourthchurch.org/'
CONCERTS_URL = 'https://www.fourthchurch.org/concerts/'
SOURCE = 'Fourth Presbyterian Church'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'([A-Z][a-z]+\s+\d{1,2})(?:,\s*(\d{4}))?\s*$'
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\b', re.IGNORECASE)
VENUE_RE = re.compile(r'\b(Sanctuary|Buchanan Chapel|by the Fountain|Fountain)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{2,}', '\n', text).strip()


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    normalized = re.sub(r'\.', '', match.group(1)).replace(' ', '').upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_event(paragraph, current_year):
    lines = [clean_text(line) for line in paragraph.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 2:
        return None

    date_match = DATE_RE.match(lines[0])
    if not date_match:
        return None
    year = date_match.group(2) or current_year
    if not year:
        return None
    try:
        event_date = datetime.strptime(
            f'{date_match.group(1)} {year}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None

    detail = clean_text('\n'.join(lines[1:]))
    if re.search(r'\bno concert\b', detail, re.IGNORECASE):
        return None

    venue_match = VENUE_RE.search(detail)
    if not venue_match:
        return None
    raw_venue = venue_match.group(1).lower()
    venue = 'Fourth Presbyterian Church Sanctuary'
    if 'fountain' in raw_venue:
        venue = 'Fourth Presbyterian Church Michigan Avenue Courtyard Fountain'
    elif 'buchanan' in raw_venue:
        venue = 'Buchanan Chapel, Fourth Presbyterian Church'

    time_from = parse_time(detail) or '12:10'
    title_text = TIME_RE.sub('', detail)
    title_text = VENUE_RE.sub('', title_text)
    title = clean_text(title_text).replace('\n', ' ')
    title = re.sub(r'\s{2,}', ' ', title).strip(' .,;-')
    if not title:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': CONCERTS_URL,
        'time_from': time_from,
        'venue': venue,
        'city': 'Chicago',
        'country_code': 'US',
        'description': detail,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CONCERTS_URL, timeout=45)
    response.raise_for_status()
    # The legacy page omits several closing paragraph tags. Browsers and lxml
    # recover them as sibling event blocks; Python's built-in parser nests the
    # remainder of the schedule inside one paragraph.
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, 'lxml')
    content = soup.select_one('#body-content')
    if not content:
        log_message(
            'Concert listing container not found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
        return []

    records = []
    current_year = None
    for node in content.find_all(['h2', 'p'], recursive=False):
        text = clean_text(node.get_text(' ', strip=True))
        month_match = re.fullmatch(r'[A-Z][a-z]+\s+(\d{4})', text)
        if month_match:
            current_year = month_match.group(1)
            continue
        record = parse_event(node, current_year)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No valid concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class FourthChurchOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fourthchurch_org',
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
        return scrape_events()


def main():
    FourthChurchOrgCrawler().run()


if __name__ == '__main__':
    main()
