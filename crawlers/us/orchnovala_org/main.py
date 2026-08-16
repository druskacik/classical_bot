import re
from datetime import datetime
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.orchnovala.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts'
ARCHIVE_URL = f'{SOURCE_URL}concerts-1'
SOURCE = 'Orchestra Nova LA'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_PATTERN = re.compile(r'^[A-Z][a-z]+ \d{1,2}, 20\d{2}$')
CITY_PATTERN = re.compile(r'^(.+?),\s*CA\s+\d{5}(?:-\d{4})?$', re.MULTILINE)
TIME_PATTERN = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP]M)\b', re.IGNORECASE)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_PATTERN.search(value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour not in range(1, 13) or minute not in range(60):
        return None
    if match.group(3).upper() == 'PM' and hour != 12:
        hour += 12
    elif match.group(3).upper() == 'AM' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_location(block):
    text = clean_text(block)
    city_match = CITY_PATTERN.search(text)
    strong_lines = [clean_text(item) for item in block.select('strong')]
    strong_lines = [line for line in strong_lines if line]
    if not strong_lines:
        strong_lines = text.splitlines()[:1]
    if not city_match or not strong_lines or not strong_lines[0]:
        return None
    return ' – '.join(strong_lines), city_match.group(1).strip()


def event_fragment(title):
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    return f'{CONCERTS_URL}#{quote(slug)}'


def parse_archive_event(blocks):
    if len(blocks) < 3:
        return None
    location = parse_location(blocks[0])
    schedule = clean_text(blocks[1])
    date_match = re.search(r'\b(\d{1,2}\.\d{1,2}\.\d{2})\b', schedule)
    detail = blocks[2]
    title = clean_text(detail.select_one('h1, h2, h3, h4, h5, h6'))
    if not location or not date_match or not title:
        return None
    try:
        event_date = datetime.strptime(date_match.group(1), '%m.%d.%y').date().isoformat()
    except ValueError:
        return None

    venue, city = location
    detail_text = clean_text(detail)
    description = detail_text[len(title):].strip() if detail_text.startswith(title) else detail_text
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    return {
        'title': title,
        'date': event_date,
        'url': f'{ARCHIVE_URL}#{quote(slug)}',
        'time_from': parse_time(schedule),
        'venue': venue,
        'city': city,
        'description': description or None,
    }


def parse_event(blocks, index):
    event_date = parse_date(clean_text(blocks[index]))
    if not event_date or index + 3 >= len(blocks):
        return None

    time_from = parse_time(clean_text(blocks[index + 1]))
    location = parse_location(blocks[index + 2])
    detail = blocks[index + 3]
    title = clean_text(detail.select_one('h1, h2, h3, h4, h5, h6'))
    if not title or not location:
        return None

    venue, city = location
    detail_text = clean_text(detail)
    description = detail_text[len(title):].strip() if detail_text.startswith(title) else detail_text
    return {
        'title': title,
        'date': event_date,
        'url': event_fragment(title),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': description or None,
    }


class OrchnovalaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchnovala_org',
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
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        pages = {}
        for url in (CONCERTS_URL, ARCHIVE_URL):
            try:
                response = requests.get(url, headers=HEADERS, timeout=45)
                response.raise_for_status()
                pages[url] = BeautifulSoup(response.text, 'html.parser')
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Orchestra Nova LA concerts',
                    event='crawler_fetch_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

        soup = pages[CONCERTS_URL]
        blocks = [block for block in soup.select('main .sqs-html-content') if clean_text(block)]
        records = []
        for index, block in enumerate(blocks):
            if DATE_PATTERN.fullmatch(clean_text(block)):
                record = parse_event(blocks, index)
                if record:
                    records.append(record)

        archive_blocks = [
            block
            for block in pages[ARCHIVE_URL].select('main .sqs-html-content')
            if clean_text(block)
        ]
        archive_record = parse_archive_event(archive_blocks)
        if archive_record:
            records.append(archive_record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    OrchnovalaOrgCrawler().run()


if __name__ == '__main__':
    main()
