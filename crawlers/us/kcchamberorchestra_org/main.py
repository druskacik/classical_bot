import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kcchamberorchestra.org/'
SOURCE = 'Kansas City Chamber Orchestra'
LISTING_URLS = (
    f'{SOURCE_URL}current-season-concerts',
    f'{SOURCE_URL}past-season-concerts',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'((?:January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+\d{1,2},\s+\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?\b', re.IGNORECASE)
CITY_RE = re.compile(
    r'(?:^|,\s*)([A-Za-z][A-Za-z .\'-]+),\s*(MO|KS|Kansas|Missouri)\s*$',
    re.IGNORECASE,
)
SEASON_RE = re.compile(r'^\d{4}\s*[–-]\s*\d{4}(?:\s+Concert Details)?$', re.IGNORECASE)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
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
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour not in range(1, 13) or minute not in range(60):
        return None
    if match.group(3).lower() == 'p' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'a' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def content_blocks(soup):
    main = soup.find('main')
    if not main:
        return []
    blocks = []
    for node in main.select('h1, h2, h3, h4, h5, h6, p'):
        text = clean_text(node.get_text('\n', strip=True))
        if text and (not blocks or text != blocks[-1]):
            blocks.append(text)
    return blocks


def city_from_block(value):
    for line in value.splitlines():
        match = CITY_RE.search(clean_text(line))
        if match:
            return match.group(1).strip()
    return None


def looks_like_address(value):
    return bool(re.match(r'^\d+\s', value) or re.search(r'\b(?:St|Street|Rd|Road|Blvd|Pkwy)\b', value))


def records_from_blocks(blocks, url):
    date_indexes = [index for index, block in enumerate(blocks) if parse_date(block)]
    records = []
    previous_title = None

    for position, date_index in enumerate(date_indexes):
        event_date = parse_date(blocks[date_index])
        title_index = date_index - 1
        title = clean_text(blocks[title_index]) if title_index >= 0 else ''
        if re.search(r'\b(?:free|community)\b.*\b(?:concert|outdoor)\b', title, re.IGNORECASE):
            title_index -= 1
            title = clean_text(blocks[title_index]) if title_index >= 0 else title
        if not title or SEASON_RE.match(title) or parse_time(title) or city_from_block(title):
            title = previous_title or ''
            title_index = date_index
        if not title or len(title) > 180:
            continue

        next_title_index = (
            date_indexes[position + 1] - 1 if position + 1 < len(date_indexes) else len(blocks)
        )
        details = blocks[date_index:min(next_title_index, date_index + 8)]
        time_from = next((parse_time(value) for value in details if parse_time(value)), None)

        city = None
        city_offset = None
        for offset, value in enumerate(details):
            city = city_from_block(value)
            if city:
                city_offset = offset
                break
        if not city:
            continue

        venue = ''
        candidates = []
        date_tail = DATE_RE.sub('', blocks[date_index], count=1)
        date_tail = TIME_RE.sub('', date_tail, count=1).strip(' ,-\n')
        if date_tail:
            candidates.extend(date_tail.splitlines())
        if city_offset is not None:
            for value in details[1:city_offset]:
                candidates.extend(value.splitlines())
        for candidate in candidates:
            candidate = clean_text(candidate)
            if (
                candidate
                and re.search(r'[A-Za-z0-9]', candidate)
                and not parse_time(candidate)
                and not looks_like_address(candidate)
            ):
                venue = candidate
                break
        if not venue:
            continue

        description_start = date_index + (city_offset or 0) + 1
        description_parts = [
            value for value in blocks[description_start:next_title_index]
            if value and value not in {venue, city} and not looks_like_address(value)
        ]
        description = '\n\n'.join(description_parts) or None
        records.append({
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
        })
        previous_title = title

    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in LISTING_URLS:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Concert listing request failed',
                event='crawler_request_failed',
                level='error',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        page_records = records_from_blocks(content_blocks(BeautifulSoup(response.text, 'html.parser')), url)
        log_message(
            'Concert listing parsed',
            event='crawler_listing_parsed',
            url=url,
            record_count=len(page_records),
        )
        records.extend(page_records)

    unique = {(item['title'], item['date'], item['time_from'], item['venue']): item for item in records}
    return sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['venue']))


class KcChamberOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kcchamberorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    KcChamberOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
