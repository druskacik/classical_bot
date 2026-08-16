import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://missoulasymphony.org/'
SOURCE = 'Missoula Symphony'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
EVENT_CATEGORY_ID = 9

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?P<date>[A-Z][a-z]{2,8}\.? ?\s+\d{1,2},\s+\d{4})'
    r'\s*/\s*'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.IGNORECASE,
)

SECTION_ENDINGS = {
    'buy tickets',
    'buy tickets now - click here',
    'concert week activities',
    'student night @dress rehearsal',
}

CITY_BY_VENUE = {
    'KettleHouse Amphitheater': 'Bonner',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def parse_date(value):
    value = re.sub(r'(?<=\w)\.', '', value)
    for pattern in ('%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    value = re.sub(r'\s+', ' ', value).strip().upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def section_lines(lines, headings):
    start = next(
        (index for index, line in enumerate(lines) if line.lower() in headings),
        None,
    )
    if start is None:
        return []

    values = []
    for line in lines[start + 1:]:
        if line.lower() == 'location':
            break
        values.append(line)
    return values


def parse_venue(lines):
    start = next(
        (index for index, line in enumerate(lines) if line.lower() == 'location'),
        None,
    )
    if start is None:
        return ''

    values = []
    for line in lines[start + 1:]:
        if line.lower() in SECTION_ENDINGS:
            break
        values.append(line)
        if len(values) == 2:
            break

    if values == ['University of Montana', 'Dennison Theatre']:
        return 'Dennison Theatre, University of Montana'
    return ', '.join(values)


def records_from_post(post):
    title = clean_text(post.get('title', {}).get('rendered'))
    description = clean_text(post.get('content', {}).get('rendered'))
    url = post.get('link', '').strip()
    lines = description.splitlines()
    venue = parse_venue(lines)
    date_lines = section_lines(lines, {'date/time', 'date / time', 'dates/times'})

    if not title or not url.startswith(('http://', 'https://')) or not venue or not date_lines:
        return []

    city = CITY_BY_VENUE.get(venue, 'Missoula')
    records = []
    for line in date_lines:
        match = DATE_TIME_RE.search(line)
        if not match:
            continue
        event_date = parse_date(match.group('date'))
        time_from = parse_time(match.group('time'))
        if not event_date or not time_from:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'categories': EVENT_CATEGORY_ID,
                'per_page': 100,
                'page': page,
                'orderby': 'date',
                'order': 'asc',
            },
            timeout=45,
        )
        response.raise_for_status()
        posts = response.json()
        for post in posts:
            records.extend(records_from_post(post))

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No concrete concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['url']),
    )


class MissoulaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='missoulasymphony_org',
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
    MissoulaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
