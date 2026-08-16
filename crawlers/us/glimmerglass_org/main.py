import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://glimmerglass.org/'
CALENDAR_URL = f'{SOURCE_URL}calendar/'
SOURCE = 'The Glimmerglass Festival'
CITY = 'Cooperstown'
VENUE = 'The Glimmerglass Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

CALENDAR_DATA_RE = re.compile(
    r'var\s+calendarDateData\s*=\s*(\{.*?\});', re.DOTALL
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def extract_calendar_data(html):
    match = CALENDAR_DATA_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def parse_starttime(value):
    value = re.sub(r'\s+America/New_York$', '', value or '').strip()
    try:
        parsed = datetime.strptime(value, '%m/%d/%Y %I:%M %p')
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def description_from_event(event):
    parts = []
    for field in ('long_desc', 'short_desc', 'post_content'):
        text = clean_text(event.get(field))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def record_from_performance(performance):
    event = performance.get('mc_event') or {}
    title = clean_text(event.get('post_title'))
    parsed_start = parse_starttime(performance.get('starttime'))
    url = performance.get('event_permalink')
    if not url:
        slug = clean_text(event.get('post_name'))
        url = f'{SOURCE_URL}events/{slug}/' if slug else ''

    if not title or not parsed_start or not url.startswith(('http://', 'https://')):
        return None

    event_date, time_from = parsed_start
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': description_from_event(event),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None, years=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    current_year = datetime.now().year
    years = years or range(current_year - 1, current_year + 3)

    records = []
    for year in years:
        for month in ('June', 'July', 'August', 'September'):
            month_value = f'{month} {year}'
            try:
                response = session.get(
                    CALENDAR_URL,
                    params={'month': month_value},
                    timeout=45,
                )
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Calendar month request failed',
                    event='crawler_request_failed',
                    level='warning',
                    url=CALENDAR_URL,
                    month=month_value,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            data = extract_calendar_data(response.text)
            if not data:
                continue

            for day in data.get('dates', {}).values():
                for performance in day.get('performances', []):
                    record = record_from_performance(performance)
                    if record:
                        records.append(record)

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    result = sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )
    if not result:
        log_message(
            'No calendar performances found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return result


class GlimmerglassOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='glimmerglass_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
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
    GlimmerglassOrgCrawler().run()


if __name__ == '__main__':
    main()
