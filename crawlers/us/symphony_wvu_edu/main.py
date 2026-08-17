import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://symphony.wvu.edu/'
EVENTS_URL = urljoin(SOURCE_URL, 'upcoming-events')
SOURCE = 'WVU Symphony Orchestra'
HOME_CITY = 'Morgantown'
HOME_VENUE = 'Lyell B. Clay Concert Theatre'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

SEASON_RE = re.compile(r'\b(20\d{2})\s*[-\N{EN DASH}]\s*(\d{2,4})\b')
EVENT_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\s+at\s+'
    r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*'
    r'(?P<period>[ap])\.?\s*m\.?',
    re.IGNORECASE,
)


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def season_years(value):
    match = SEASON_RE.search(value or '')
    if not match:
        return None
    start = int(match.group(1))
    end_text = match.group(2)
    end = int(end_text) if len(end_text) == 4 else (start // 100) * 100 + int(end_text)
    if end < start:
        end += 100
    return start, end


def parse_occurrence(value, years):
    match = EVENT_RE.search(value)
    if not match or not years:
        return None

    try:
        month = datetime.strptime(match.group('month'), '%B').month
        year = years[0] if month >= 7 else years[1]
        event_date = datetime(year, month, int(match.group('day'))).date().isoformat()
        hour = int(match.group('hour')) % 12
        if match.group('period').lower() == 'p':
            hour += 12
        time_from = f"{hour:02d}:{int(match.group('minute') or 0):02d}"
    except ValueError:
        return None
    return event_date, time_from


def event_title(value):
    suffix = clean_text(value.split('-', 1)[1]) if '-' in value else ''
    if suffix and not re.search(r'\b(?:school|hall|theatre|theater)\b.*\bin\b', suffix, re.I):
        suffix = re.sub(r'\s*\(ticketed event\)\s*$', '', suffix, flags=re.I)
        if suffix:
            return suffix
    return 'WVU Symphony Orchestra Concert'


def event_location(value):
    if re.search(r'John Marshall High School', value, re.I):
        return 'John Marshall High School', 'Wheeling'
    return HOME_VENUE, HOME_CITY


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    main = soup.select_one('main#maincontent') or soup.select_one('main')
    intro = clean_text(main.find('p').get_text(' ', strip=True)) if main and main.find('p') else ''
    years = season_years(intro)
    records = []

    for item in main.select('ul li') if main else []:
        text = clean_text(item.get_text(' ', strip=True))
        occurrence = parse_occurrence(text, years)
        if not occurrence:
            continue

        event_date, time_from = occurrence
        venue, city = event_location(text)
        visible_link = next(
            (link for link in item.select('a[href]') if clean_text(link.get_text(' ', strip=True))),
            None,
        )
        url = urljoin(EVENTS_URL, visible_link['href']) if visible_link else EVENTS_URL
        description = '\n\n'.join(part for part in (text, intro) if part)
        records.append({
            'title': event_title(text),
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

    if not records:
        log_message(
            'No WVU Symphony concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )

    unique = {
        (record['date'], record['time_from'], record['venue'], record['title']): record
        for record in records
    }
    return sorted(unique.values(), key=lambda record: (record['date'], record['time_from'], record['title']))


class SymphonyWvuEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='symphony_wvu_edu',
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
    SymphonyWvuEduCrawler().run()


if __name__ == '__main__':
    main()
