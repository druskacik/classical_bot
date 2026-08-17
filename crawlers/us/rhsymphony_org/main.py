import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://rhsymphony.org/'
LISTING_URL = urljoin(SOURCE_URL, 'upcoming-events-1')
SOURCE = 'Rock Hill Symphony Orchestra'
CITY = 'Rock Hill'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

CALENDAR_PROPS_RE = re.compile(
    r"componentName:'@widget/CALENDAR/bs-calendar',props:JSON\.parse\("
    r'(\"(?:\\.|[^\"])*\")\)'
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_description(value):
    if not value:
        return None
    try:
        content = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return clean_text(value) or None

    paragraphs = []
    for block in content.get('blocks', []):
        text = clean_text(block.get('text'))
        if text:
            paragraphs.append(text)
    return '\n\n'.join(paragraphs) or None


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%m/%d/%Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    text = clean_text(value)
    patterns = [
        r'(?i)(?:concert|program)\s+(?:begins(?:\s+at)?|at)\s+'
        r'(\d{1,2}(?::\d{2})?\s*[ap]m)',
        r'(?i)^\s*(\d{1,2}(?::\d{2})?\s*[ap]m)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        value = re.sub(r'(?i)\s*([ap]m)$', r' \1', match.group(1)).upper()
        for time_format in ('%I:%M %p', '%I %p'):
            try:
                return datetime.strptime(value, time_format).strftime('%H:%M')
            except ValueError:
                pass
    return None


def extract_calendar_events(session, html):
    soup = BeautifulSoup(html, 'html.parser')
    script_urls = [
        urljoin(LISTING_URL, script.get('src'))
        for script in soup.find_all('script', src=True)
        if '/gpub/' in script.get('src', '')
    ]

    for script_url in script_urls:
        response = session.get(script_url, timeout=45)
        response.raise_for_status()
        match = CALENDAR_PROPS_RE.search(response.text)
        if not match:
            continue
        try:
            props = json.loads(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
        events = props.get('manualEvents')
        if isinstance(events, list):
            return events
    return []


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()

    events = extract_calendar_events(session, response.text)
    records = []
    for event in events:
        title = clean_text(event.get('title'))
        event_date = parse_date(event.get('date'))
        venue = clean_text(event.get('location'))
        if not title or not event_date or not venue:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': LISTING_URL,
            'time_from': parse_time(event.get('start')),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': parse_description(event.get('desc')),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['venue']))


class RhSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rhsymphony_org',
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
    RhSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
