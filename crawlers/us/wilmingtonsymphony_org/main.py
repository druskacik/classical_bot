import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wilmingtonsymphony.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
YOUTH_CALENDAR_URL = urljoin(SOURCE_URL, 'wsyo-events')
SOURCE = 'Wilmington Symphony Orchestra'
LOCAL_TIMEZONE = ZoneInfo('America/New_York')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_PATTERN = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})\s*\|\s*'
    r'(.+?)\s*\|\s*(.+)$'
)
TIME_PATTERN = re.compile(r'(\d{1,2}):([0-5]\d)\s*([AP]M)', re.I)
IN_SCOPE_SECTIONS = {
    'Classics Series',
    'Youth Concerts',
    'Family-Friendly',
    'CFCC’s Wilson Center Presents',
}
CALENDAR_CANDIDATE_PATTERN = re.compile(
    r'\b(concert|concerto competition|recital|performance)\b', re.I
)


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = TIME_PATTERN.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'PM':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    blocks = [clean_text(block) for block in soup.select('main .sqs-html-content')]
    records = []

    for index, text in enumerate(blocks):
        match = DATE_PATTERN.search(text.replace('\n', ' | '))
        if not match:
            continue

        previous_date = max(
            (position for position in range(index) if DATE_PATTERN.search(blocks[position].replace('\n', ' | '))),
            default=-1,
        )
        section_index = next(
            (position for position in range(index - 1, previous_date, -1)
             if blocks[position] in IN_SCOPE_SECTIONS),
            None,
        )
        if section_index is None or section_index + 1 >= index:
            continue

        title = re.sub(r'\s+', ' ', blocks[section_index + 1]).strip()
        venue = match.group(3).strip()
        try:
            event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        except ValueError:
            continue
        if not title or not venue:
            continue

        description_parts = blocks[section_index + 2:index]
        if index + 1 < len(blocks):
            description_parts.append(blocks[index + 1])
        description = '\n\n'.join(part for part in description_parts if part) or None
        # Rebuild each matched time for pages that publish two performances on
        # the same date.
        times = [
            parse_time(f'{hour}:{minute} {meridiem}')
            for hour, minute, meridiem in TIME_PATTERN.findall(match.group(2))
        ]
        times = [value for value in times if value] or [None]
        for time_from in times:
            records.append({
                'title': title,
                'date': event_date,
                'url': CONCERTS_URL,
                'time_from': time_from,
                'venue': venue,
                'city': 'Wilmington',
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def city_from_location(location):
    address = clean_text(location.get('addressLine2'))
    match = re.match(r'([^,]+?)(?:,?\s+[A-Z]{2}\b|,)', address)
    return match.group(1).strip() if match else ''


def record_from_calendar_item(item):
    title = clean_text(item.get('title'))
    if not CALENDAR_CANDIDATE_PATTERN.search(title):
        return None
    path = item.get('fullUrl')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    try:
        moment = datetime.fromtimestamp(
            int(item.get('startDate')) / 1000, tz=LOCAL_TIMEZONE
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if not title or not path or not venue or not city:
        return None
    description = clean_text(item.get('body')) or clean_text(item.get('excerpt')) or None
    return {
        'title': title,
        'date': moment.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': moment.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_youth_calendar(session):
    records = []
    url = f'{YOUTH_CALENDAR_URL}?format=json'
    page = 1
    while url:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        data = response.json()
        items = data.get('past') or []
        if page == 1:
            items = (data.get('upcoming') or []) + items
        for item in items:
            record = record_from_calendar_item(item)
            if record:
                records.append(record)

        next_url = (data.get('pagination') or {}).get('nextPageUrl')
        url = urljoin(SOURCE_URL, next_url) if next_url else None
        if url:
            url += '&format=json' if '?' in url else '?format=json'
        page += 1
        if page > 100:
            raise RuntimeError('Youth calendar pagination exceeded 100 pages')
    return records


class WilmingtonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wilmingtonsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(CONCERTS_URL, timeout=45)
        response.raise_for_status()
        records = parse_listing(response.text)
        records.extend(scrape_youth_calendar(session))
        if not records:
            log_message(
                'No Wilmington Symphony concert candidates found',
                event='crawler_empty_listing',
                level='warning',
                url=CONCERTS_URL,
                record_count=0,
            )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    WilmingtonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
