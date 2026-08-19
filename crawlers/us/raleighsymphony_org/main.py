import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.raleighsymphony.org/'
SEASON_URL = f'{SOURCE_URL}currentseason'
SOURCE = 'Raleigh Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

EVENT_LINE_RE = re.compile(
    r'(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+20\d{2})\s*\|\s*'
    r'(?P<time>\d{1,2}:\d{2}\s*[ap]m)\s*\|\s*(?P<venue>.+)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if isinstance(value, Tag) else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_event_line(value):
    match = EVENT_LINE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        event_date = datetime.strptime(match.group('date'), '%B %d, %Y').date().isoformat()
        event_time = datetime.strptime(
            re.sub(r'\s+', '', match.group('time')).upper(), '%I:%M%p'
        ).strftime('%H:%M')
    except ValueError:
        return None
    venue = clean_text(match.group('venue')).strip(' |')
    return event_date, event_time, venue


def event_description(container, metadata_heading):
    parts = []
    for element in metadata_heading.find_next_siblings():
        if element.name not in {'p', 'ul', 'ol'}:
            continue
        text = clean_text(element)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_events(main_content):
    soup = BeautifulSoup(main_content or '', 'html.parser')
    records = []
    for container in soup.select('.sqs-html-content'):
        title_heading = container.find('h1')
        metadata_heading = container.find('h2')
        if not title_heading or not metadata_heading:
            continue
        parsed = parse_event_line(metadata_heading)
        if not parsed:
            continue
        title = clean_text(title_heading)
        event_date, time_from, venue = parsed
        if not title or not venue:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': SEASON_URL,
            'time_from': time_from,
            'venue': venue,
            'city': 'Raleigh',
            'country_code': 'US',
            'description': event_description(container, metadata_heading),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class RaleighSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='raleighsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(
            SEASON_URL,
            params={'format': 'json'},
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        records = parse_events(payload.get('mainContent'))
        if not records:
            log_message(
                'No Raleigh Symphony concert blocks found',
                event='crawler_empty_result',
                level='warning',
                url=response.url,
                record_count=0,
                error_type='NoConcertBlocks',
                error_message='The current-season JSON contained no parseable event blocks',
            )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    RaleighSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
