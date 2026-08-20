import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://yorksymphony.org/'
SOURCE = 'York Symphony Orchestra'
API_URL = 'https://yorksymphony.org/wp-json/tribe/events/v1/events'

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_description(value):
    if not value:
        return None
    value = html.unescape(str(value))
    value = re.sub(r'\[/?et_pb[^\]]*\]', ' ', value, flags=re.IGNORECASE)
    text = BeautifulSoup(value, 'html.parser').get_text('\n')
    text = text.replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text).strip()
    return text or None


def parse_displayed_date(lines):
    for index, line in enumerate(lines):
        if line.casefold() != 'date':
            continue
        for candidate in lines[index + 1:index + 4]:
            candidate = re.sub(r'(?<=\d)(st|nd|rd|th)\b', '', candidate)
            for date_format in ('%A, %B %d, %Y', '%B %d, %Y'):
                try:
                    return datetime.strptime(candidate, date_format).date()
                except ValueError:
                    pass
    return None


def parse_displayed_time(lines):
    for index, line in enumerate(lines):
        if line.casefold() != 'time':
            continue
        for candidate in lines[index + 1:index + 4]:
            match = re.search(
                r'(?<!\d)(\d{1,2})(?::(\d{2}))?'
                r'(?:\s*-\s*\d{1,2}(?::\d{2})?)?\s*([ap])\.?m\.?',
                candidate,
                re.I,
            )
            if not match:
                continue
            hour = int(match.group(1)) % 12
            if match.group(3).casefold() == 'p':
                hour += 12
            return f'{hour:02d}:{int(match.group(2) or 0):02d}'
    return None


def parse_venue(lines):
    for index, line in enumerate(lines):
        if line.casefold() != 'location':
            continue
        for candidate in lines[index + 1:index + 4]:
            if candidate.casefold() in {'address', 'date', 'time', 'duration'}:
                break
            if candidate and not re.match(r'^\d+\s', candidate):
                return candidate.strip(' ,')
    return None


def record_from_event(event):
    title = html.unescape(str(event.get('title') or '')).strip()
    url = str(event.get('url') or '').strip()
    description = clean_description(event.get('description'))
    lines = description.splitlines() if description else []
    venue = parse_venue(lines)

    try:
        starts_at = datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S')
    except (KeyError, TypeError, ValueError):
        return None

    displayed_date = parse_displayed_date(lines)
    event_date = displayed_date or starts_at.date()
    time_from = starts_at.strftime('%H:%M')
    if displayed_date and displayed_date != starts_at.date():
        time_from = parse_displayed_time(lines) or time_from

    if not title or not url or not venue:
        return None

    return {
        'title': title,
        'date': event_date.isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'York',
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    params = {
        'per_page': 50,
        'page': 1,
        'start_date': '1900-01-01 00:00:00',
        'end_date': '2100-12-31 23:59:59',
    }
    records = []

    while True:
        response = session.get(API_URL, params=params, headers=HEADERS, timeout=60)
        response.raise_for_status()
        payload = response.json()
        events = payload.get('events')
        if not isinstance(events, list):
            raise ValueError('York Symphony events API returned an unexpected response')

        for event in events:
            record = record_from_event(event)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping incomplete York Symphony event',
                    event='crawler_record_skipped',
                    level='warning',
                    url=event.get('url') or API_URL,
                )

        total_pages = int(payload.get('total_pages') or 1)
        if params['page'] >= total_pages:
            break
        params['page'] += 1

    if not records:
        log_message(
            'No York Symphony concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class YorkSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='yorksymphony_org',
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
    YorkSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
