import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://dbss.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts/'
SOURCE = 'Daytona Beach Symphony Society'
VENUE = 'News-Journal Center'
CITY = 'Daytona Beach'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>[AP]M),\s*'
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,\s*'
    r'(?P<year>\d{4})',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    text = ''.join(character for character in text if character.isprintable())
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None, None

    parts = match.groupdict()
    try:
        event_date = datetime.strptime(
            f"{parts['month']} {parts['day']} {parts['year']}", '%B %d %Y'
        ).date().isoformat()
        event_time = datetime.strptime(
            f"{parts['hour']}:{parts['minute'] or '00'} {parts['period']}", '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None, None
    return event_date, event_time


def parse_section(section):
    section_id = clean_text(section.get('id'))
    headings = [clean_text(node) for node in section.select('h1, h2, h3, h4, h5, h6')]
    headings = [heading for heading in headings if heading]
    full_text = clean_text(section)
    event_date, time_from = parse_date_time(full_text)
    if not section_id or not headings or not event_date:
        return None

    title_parts = []
    for heading in headings:
        if not DATE_TIME_RE.search(heading) and heading.lower() not in {'tickets', 'buy tickets'}:
            if heading not in title_parts:
                title_parts.append(heading)
    title = ' — '.join(title_parts)
    if not title:
        return None

    description = re.sub(
        r'\s*(?:Tickets|Buy Tickets)\s*$', '', full_text, flags=re.I
    ).strip()

    return {
        'title': title,
        'date': event_date,
        'url': f'{CONCERTS_URL}#{section_id}',
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(CONCERTS_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    for section in soup.select('main .fusion-fullwidth[id]'):
        record = parse_section(section)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No concert sections found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class DbssOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='dbss_org',
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
    DbssOrgCrawler().run()


if __name__ == '__main__':
    main()
