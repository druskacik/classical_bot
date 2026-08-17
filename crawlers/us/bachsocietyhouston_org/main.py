import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bachsocietyhouston.org/'
SEASON_URL = f'{SOURCE_URL}2025-2026season'
SOURCE = 'Bach Society Houston'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]+ \d{1,2}, \d{4})\s*\|\s*'
    r'(\d{1,2}(?::\d{2})?)\s*([AP]M)?\s*[\u2013-]'
    r'\s*\d{1,2}(?::\d{2})?\s*([AP]M)$',
)


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def parse_date_time(value):
    match = DATE_RE.match(clean_text(value))
    if not match:
        return None
    date_text, time_text, start_meridiem, end_meridiem = match.groups()
    meridiem = start_meridiem or end_meridiem
    try:
        event_date = datetime.strptime(date_text, '%B %d, %Y').date().isoformat()
        time_from = datetime.strptime(
            f'{time_text} {meridiem}', '%I:%M %p' if ':' in time_text else '%I %p'
        ).strftime('%H:%M')
    except ValueError:
        return None
    return event_date, time_from


def event_container(date_node):
    rich_text = date_node.find_parent('div', class_='wixui-rich-text')
    if not rich_text:
        return None
    # Each performance is one Wix column containing several rich-text blocks.
    return rich_text.find_parent('div', class_='wixui-column-strip__column')


def parse_event(date_node):
    date_text = clean_text(str(date_node))
    parsed = parse_date_time(date_text)
    container = event_container(date_node)
    if not parsed or not container:
        return None

    blocks = []
    for block in container.select('h1, h2, h3, h4, h5, h6, p'):
        text = clean_text(block.get_text(' ', strip=True))
        if text and text not in blocks:
            blocks.append(text)

    date_index = next(
        (index for index, text in enumerate(blocks) if text.startswith(date_text)), None
    )
    if date_index is None or date_index == 0:
        return None

    title = clean_text(' '.join(blocks[:date_index]))
    venue = clean_text(blocks[date_index][len(date_text):])
    description_index = date_index + 1
    if not venue and description_index < len(blocks):
        venue = blocks[description_index]
        description_index += 1
    description_parts = []
    for text in blocks[description_index:]:
        if re.match(r'^(?:Tickets?|Admission):?', text, re.IGNORECASE):
            break
        description_parts.append(text)

    if not title or not venue:
        return None
    event_date, time_from = parsed
    return {
        'title': title,
        'date': event_date,
        'url': SEASON_URL,
        'time_from': time_from,
        'venue': venue,
        'city': 'Houston',
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SEASON_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    seen = set()
    for node in soup.find_all(string=re.compile(DATE_RE)):
        record = parse_event(node)
        if not record:
            continue
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        if key in seen:
            continue
        seen.add(key)
        records.append(record)

    if not records:
        log_message(
            'No concrete performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SEASON_URL,
            record_count=0,
        )
    return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


class BachSocietyHoustonOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bachsocietyhouston_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BachSocietyHoustonOrgCrawler().run()


if __name__ == '__main__':
    main()
