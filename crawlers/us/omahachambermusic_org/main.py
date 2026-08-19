import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.omahachambermusic.org/'
SOURCE = 'Omaha Chamber Music Society'
SERIES_URLS = [
    f'{SOURCE_URL}heritage-series',
    f'{SOURCE_URL}summer-concert-series',
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),\s+(\d{4})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', re.IGNORECASE)
ADDRESS_CITY_RE = re.compile(
    r'\b\d{1,6}\s+[^\n,]+,\s*([A-Za-z][A-Za-z .\'’/-]+?)(?:,\s*[A-Z]{2})?\s*$',
    re.MULTILINE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def metadata_block(row):
    for block in row.select('.sqs-block-html'):
        if DATE_RE.search(clean_text(block.get_text('\n', strip=True))):
            return block
    return None


def parse_row(row, page_url):
    block = metadata_block(row)
    if not block:
        return None

    metadata = clean_text(block.get_text('\n', strip=True))
    title_node = block.select_one('h1, h2, h3, h4')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    event_date = parse_date(metadata)

    paragraphs = [clean_text(node.get_text('\n', strip=True)) for node in block.select('p')]
    date_index = next((index for index, value in enumerate(paragraphs) if DATE_RE.search(value)), None)
    location = paragraphs[date_index + 1] if date_index is not None and date_index + 1 < len(paragraphs) else ''
    location_lines = [line for line in location.splitlines() if line]
    venue = location_lines[0] if location_lines else ''
    city_match = ADDRESS_CITY_RE.search(location)
    city = clean_text(city_match.group(1)) if city_match else ''

    # The organization presents these series in Omaha and currently prints the
    # city in the address line. Use its stable home-city context only when an
    # address is present but the final city token is omitted.
    if not city and any(char.isdigit() for char in location):
        city = 'Omaha'

    description_parts = []
    for candidate in row.select('.sqs-block-html'):
        if candidate is block:
            continue
        value = clean_text(candidate.get_text('\n', strip=True))
        if value:
            description_parts.append(value)
    description = clean_text('\n\n'.join(description_parts)) or None

    if not all((title, event_date, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': page_url,
        'time_from': parse_time(metadata),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_page(page_url, session):
    response = session.get(page_url, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    payload = response.json()
    soup = BeautifulSoup(payload.get('mainContent', ''), 'html.parser')
    records = []
    dated_rows = 0
    for row in soup.select('.row.sqs-row'):
        if not DATE_RE.search(clean_text(row.get_text('\n', strip=True))):
            continue
        # Squarespace may nest grid rows. Only parse the innermost dated row,
        # otherwise the same event is seen again through its outer container.
        if any(
            DATE_RE.search(clean_text(child.get_text('\n', strip=True)))
            for child in row.select('.row.sqs-row')
        ):
            continue
        dated_rows += 1
        record = parse_row(row, page_url)
        if record:
            records.append(record)

    if dated_rows > len(records):
        log_message(
            'Skipped dated concert rows missing required fields',
            event='crawler_records_skipped',
            level='warning',
            url=page_url,
            record_count=dated_rows - len(records),
        )
    return records


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for page_url in SERIES_URLS:
        records.extend(scrape_page(page_url, session))

    if not records:
        log_message(
            'No valid concerts found on series pages',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class OmahaChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='omahachambermusic_org',
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
        return scrape_events()


def main():
    OmahaChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
