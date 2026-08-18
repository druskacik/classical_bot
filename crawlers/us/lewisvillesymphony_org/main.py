import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lewisvillesymphony.org/'
SOURCE = 'Lewisville Lake Symphony'

SYMPHONY_VENUE = 'Lewisville Grand Theater'
SYMPHONY_CITY = 'Lewisville'
CHAMBER_VENUE = 'Trinity Presbyterian Church'
CHAMBER_CITY = 'Flower Mound'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})')
TIME_RE = re.compile(r'(\d{1,2}(?::\d{2})?\s*[AP]M)', re.I)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(match.group(1).upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def rich_text_blocks(soup):
    return [
        clean_text(node.get_text(' ', strip=True))
        for node in soup.select('[data-testid="richTextElement"]')
    ]


def section_after(blocks, heading, stop_heading):
    starts = [index for index, text in enumerate(blocks) if text == heading]
    if not starts:
        return []
    start = starts[-1] + 1
    stop = next(
        (index for index in range(start, len(blocks)) if blocks[index].startswith(stop_heading)),
        len(blocks),
    )
    return blocks[start:stop]


def dated_groups(blocks):
    groups = []
    current = []
    for text in blocks:
        if parse_date(text):
            if current:
                groups.append(current)
            current = [text]
        elif current:
            current.append(text)
    if current:
        groups.append(current)
    return groups


def make_record(group, venue, city, default_time='19:30'):
    event_date = parse_date(group[0])
    first = DATE_RE.sub('', group[0]).strip(' ,-')
    description_parts = ([first] if first else []) + group[1:]
    description_parts = [part for part in description_parts if part]
    if not event_date or not description_parts:
        return None

    title = description_parts[0]
    if re.fullmatch(r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)', title, re.I):
        if len(description_parts) < 2:
            return None
        title = description_parts[1]

    return {
        'title': title,
        'date': event_date,
        'url': SOURCE_URL,
        'time_from': parse_time(' '.join(group)) or default_time,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SOURCE_URL, timeout=60)
    response.raise_for_status()
    blocks = rich_text_blocks(BeautifulSoup(response.text, 'html.parser'))

    records = []
    symphony = section_after(blocks, 'Symphony Series', 'Peter & the Wolf')
    for group in dated_groups(symphony):
        record = make_record(group, SYMPHONY_VENUE, SYMPHONY_CITY)
        if record:
            records.append(record)

    peter_index = next((i for i, text in enumerate(blocks) if text.startswith('Peter & the Wolf')), None)
    chamber_index = next((i for i, text in enumerate(blocks) if text == 'International Chamber Series' and peter_index is not None and i > peter_index), None)
    if peter_index is not None:
        peter_stop = chamber_index if chamber_index is not None else peter_index + 1
        peter_group = blocks[peter_index:peter_stop]
        record = make_record(peter_group, 'Lamb of God Lutheran Church', 'Flower Mound', None)
        if record:
            record['title'] = 'Peter & the Wolf'
            records.append(record)

    if chamber_index is not None:
        chamber_stop = next(
            (
                index for index in range(chamber_index + 1, len(blocks))
                if blocks[index] == 'Tickets'
            ),
            len(blocks),
        )
        chamber_blocks = blocks[chamber_index + 1:chamber_stop]
        for group in dated_groups(chamber_blocks):
            record = make_record(group, CHAMBER_VENUE, CHAMBER_CITY)
            if record:
                records.append(record)

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    records = sorted(unique.values(), key=lambda item: (item['date'], item['title']))
    if not records:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return records


class LewisvilleSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lewisvillesymphony_org',
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
    LewisvilleSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
