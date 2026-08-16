import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://hillsborosymphony.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts'
SOURCE = 'Hillsboro Symphony Orchestra'
CITY = 'Hillsboro'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DETAIL_RE = re.compile(
    r'(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+at\s+'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)\s+'
    r'(?P<venue>.+?)\s+(?=\d+\s)(?P<address>.+)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.get_text(' ', strip=True)).strip()


def parse_details(value):
    match = DETAIL_RE.fullmatch(value)
    if not match:
        return None
    try:
        event_date = datetime.strptime(match.group('date'), '%B %d, %Y').date().isoformat()
        time_from = datetime.strptime(
            re.sub(r'\s+', '', match.group('time')).upper(), '%I:%M%p'
        ).strftime('%H:%M')
    except ValueError:
        return None

    address = match.group('address')
    if not re.search(r'\bHillsboro\s*,\s*OR\b', address, re.IGNORECASE):
        return None
    return event_date, time_from, match.group('venue').strip()


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CONCERTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    state_numbers = sorted({
        match.group(1)
        for node in soup.select('[data-sid^="upcoming-shows_view_"]')
        if (match := re.match(r'upcoming-shows_view_(\d+)_', node.get('data-sid', '')))
    })
    records = []
    for state in state_numbers:
        prefix = f'upcoming-shows_view_{state}_'
        fields = {
            suffix: clean_text(soup.select_one(f'[data-sid="{prefix}{suffix}"]'))
            for suffix in ('3', '4', '5', '6')
        }
        details = parse_details(fields['4'])
        if not details:
            log_message(
                'Skipping concert card without a complete date or location',
                event='crawler_record_skipped',
                level='warning',
                url=CONCERTS_URL,
                card_state=state,
            )
            continue
        event_date, time_from, venue = details
        title = fields['5']
        if not title or not venue:
            continue
        description_parts = [part for part in (fields['6'], fields['3']) if part]
        records.append({
            'title': title,
            'date': event_date,
            'url': CONCERTS_URL,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'description': '\n\n'.join(description_parts) or None,
        })

    if not records:
        log_message(
            'No complete concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class HillsboroSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hillsborosymphony_org',
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
        return scrape_concerts()


def main():
    HillsboroSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
