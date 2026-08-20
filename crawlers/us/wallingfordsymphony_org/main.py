import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wallingfordsymphony.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts'
SOURCE = 'Wallingford Symphony Orchestra'
CITY = 'Wallingford'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'^\d{2}\.\d{2}\.\d{4}$', re.MULTILINE)
DETAIL_RE = re.compile(r'^\|\s*(.+?)\s*\|\s*(.+)$')
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.I)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value, '%m.%d.%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    try:
        return datetime.strptime(
            f'{hour}:{minute or "00"} {meridiem}', '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None


def section_records(section):
    lines = [clean_text(line) for line in section.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    date_indexes = [index for index, line in enumerate(lines) if DATE_RE.fullmatch(line)]
    records = []

    for position, date_index in enumerate(date_indexes):
        end = date_indexes[position + 1] if position + 1 < len(date_indexes) else len(lines)
        chunk = lines[date_index:end]
        detail_index = next(
            (index for index, line in enumerate(chunk) if DETAIL_RE.match(line)), None
        )
        if detail_index is None:
            continue

        detail = DETAIL_RE.match(chunk[detail_index])
        venue, timing = (clean_text(value) for value in detail.groups())
        title_candidates = [
            line for line in chunk[1:detail_index]
            if not re.search(r'^(?:free concert|.+booklet.*)$', line, re.I)
        ]
        title = title_candidates[-1] if title_candidates else ''
        event_date = parse_date(chunk[0])
        if not event_date or not title or not venue:
            continue

        description_lines = chunk[detail_index + 1:]
        if len(date_indexes) == 1:
            description_lines = lines[:date_index] + description_lines
        description_lines = [
            line for line in description_lines
            if not re.search(r'^(?:buy tickets|free concert|.+booklet.*)$', line, re.I)
            and line != title
        ]
        description = '\n\n'.join(dict.fromkeys(description_lines)) or None

        records.append({
            'title': title,
            'date': event_date,
            'url': CONCERTS_URL,
            'time_from': parse_time(timing),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CONCERTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    for section in soup.select('section'):
        if DATE_RE.search(section.get_text('\n', strip=True)):
            records.extend(section_records(section))

    if not records:
        log_message(
            'No concert listings found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class WallingfordSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wallingfordsymphony_org',
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
    WallingfordSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
