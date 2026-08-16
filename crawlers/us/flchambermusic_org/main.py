import re
from datetime import datetime
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://flchambermusic.org/'
CONCERTS_URL = f'{SOURCE_URL}concerts-2/'
SOURCE = 'Florida Chamber Music Project'
DEFAULT_VENUE = 'Beaches Museum Chapel'
CITY = 'Jacksonville Beach'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|'
    r'SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2}),\s+(\d{4})\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def title_from_url(url, event_date):
    if url and url != CONCERTS_URL:
        slug = unquote(urlparse(url).path.rstrip('/').split('/')[-1])
        slug = re.sub(r'-\d{1,2}-\d{1,2}-\d{4}$', '', slug)
        words = [word for word in slug.split('-') if word.lower() not in {'fcmp', 'performs'}]
        if words:
            return f'{SOURCE}: {" ".join(words).title()}'
    return f'{SOURCE} Concert – {event_date}'


def parse_event(heading):
    event_date = parse_date(heading.get_text(' ', strip=True))
    container = heading.parent.parent if heading.parent else None
    if not event_date or not container:
        return None

    ticket_link = container.find('a', href=True)
    url = ticket_link['href'].strip() if ticket_link else CONCERTS_URL
    paragraphs = [clean_text(node.get_text(' ', strip=True)) for node in container.find_all('p')]
    paragraphs = [text for text in paragraphs if text]
    description = '\n\n'.join(dict.fromkeys(paragraphs)) or None

    venue = DEFAULT_VENUE
    if description and re.search(r"St\.?\s*Paul[’']s By the Sea Episcopal Church", description, re.I):
        venue = "St. Paul's By the Sea Episcopal Church"

    return {
        'title': title_from_url(url, event_date),
        'date': event_date,
        'url': url,
        'time_from': '15:00',
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
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
    for heading in soup.find_all('h3'):
        if not DATE_RE.search(heading.get_text(' ', strip=True)):
            continue
        record = parse_event(heading)
        if record:
            records.append(record)

    records = list({
        (record['date'], record['time_from'], record['venue'], record['title']): record
        for record in records
    }.values())
    if not records:
        log_message(
            'No concert entries found',
            event='crawler_empty_listing',
            level='warning',
            url=CONCERTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title']))


class FlchambermusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='flchambermusic_org',
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
    FlchambermusicOrgCrawler().run()


if __name__ == '__main__':
    main()
