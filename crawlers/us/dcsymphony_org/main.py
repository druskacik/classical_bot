import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://dcsymphony.org/'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Dallas Chamber Symphony'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

DATE_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),\s+(\d{4})\s+at\s+(\d{1,2}):(\d{2})\s*([ap])\.?m\.?',
    re.IGNORECASE,
)
ADDRESS_RE = re.compile(
    r'\b([A-Za-z .\'-]+),\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b'
)
VENUE_RE = re.compile(
    r'\b(hall|theatre|theater|auditorium|center|centre|church|cathedral|opera|museum)\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def get_pages(session):
    response = session.get(
        PAGES_API,
        params={
            'per_page': 100,
            'page': 1,
            '_fields': 'id,slug,link,title,content',
        },
        timeout=45,
    )
    response.raise_for_status()
    pages = response.json()
    total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
    for page_number in range(2, total_pages + 1):
        response = session.get(
            PAGES_API,
            params={
                'per_page': 100,
                'page': page_number,
                '_fields': 'id,slug,link,title,content',
            },
            timeout=45,
        )
        response.raise_for_status()
        pages.extend(response.json())
    return pages


def parse_datetime(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        event_date = datetime.strptime(
            f'{match.group(1)} {match.group(2)}, {match.group(3)}',
            '%B %d, %Y',
        ).date().isoformat()
    except ValueError:
        return None
    hour = int(match.group(4)) % 12
    if match.group(6).lower() == 'p':
        hour += 12
    return event_date, f'{hour:02d}:{match.group(5)}', match


def extract_location(lines, date_line_index):
    nearby = lines[date_line_index + 1:date_line_index + 12]
    address_index = next(
        (index for index, line in enumerate(nearby) if ADDRESS_RE.search(line)),
        None,
    )
    if address_index is None:
        return None, None

    address_match = ADDRESS_RE.search(nearby[address_index])
    city = address_match.group(1).strip(' ,')
    venue = next(
        (
            line for line in reversed(nearby[:address_index])
            if VENUE_RE.search(line) and len(line) <= 120
        ),
        None,
    )
    return venue, city


def extract_description(lines, date_line_index):
    body = lines[date_line_index + 1:]
    # Ticketing and artist biographies are repeated boilerplate. Everything
    # before them includes the event summary and complete advertised programme.
    stop = len(body)
    for marker in ('Tickets online', 'About the Artists'):
        try:
            stop = min(stop, body.index(marker))
        except ValueError:
            pass
    useful = body[:stop]
    useful = [
        line for line in useful
        if not re.search(r'^\$\d', line)
        and line not in {'Buy Tickets', 'Program'}
    ]
    description = '\n'.join(useful).strip()
    return description or None


def make_record(page):
    content = (page.get('content') or {}).get('rendered') or ''
    text = clean_text(content)
    parsed = parse_datetime(text)
    if not parsed:
        return None

    event_date, time_from, _ = parsed
    lines = text.splitlines()
    date_line_index = next(
        (index for index, line in enumerate(lines) if DATE_RE.search(line)),
        None,
    )
    if date_line_index is None:
        return None
    venue, city = extract_location(lines, date_line_index)
    title = clean_text((page.get('title') or {}).get('rendered'))
    url = page.get('link') or ''
    if not title or not url or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': extract_description(lines, date_line_index),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class DcSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dcsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            pages = get_pages(session)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to retrieve WordPress pages',
                event='crawler_request_failed',
                level='error',
                url=PAGES_API,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = [record for page in pages if (record := make_record(page))]
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    DcSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
