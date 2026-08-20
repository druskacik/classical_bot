import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.syrfcm.org/'
EVENTS_URL = f'{SOURCE_URL}concerts-and-tickets'
SOURCE = 'Syracuse Friends of Chamber Music'
VENUE = 'Grant Middle School Auditorium'
CITY = 'Syracuse'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

DATE_TIME_RE = re.compile(
    r'(?P<date>(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'\d{1,2},\s+\d{4}),\s*'
    r'(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>[ap])\.?m\.?',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def parse_event(element):
    text = clean_text(element.get_text('\n', strip=True))
    match = DATE_TIME_RE.search(text)
    if not match:
        return None

    title = clean_text(text[:match.start()]).replace('\n', ' ')
    description = clean_text(text[match.end():]) or None
    try:
        event_date = datetime.strptime(match.group('date'), '%A, %B %d, %Y').date()
    except ValueError:
        return None

    hour = int(match.group('hour')) % 12
    if match.group('meridiem').lower() == 'p':
        hour += 12
    time_from = f"{hour:02d}:{int(match.group('minute')):02d}"

    if not title:
        return None
    return {
        'title': title,
        'date': event_date.isoformat(),
        'url': EVENTS_URL,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    try:
        response = session.get(EVENTS_URL, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Syracuse Friends of Chamber Music concerts',
            event='crawler_page_failed',
            level='warning',
            url=EVENTS_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    soup = BeautifulSoup(response.text, 'html.parser')
    records = []
    for element in soup.select('[data-testid="richTextElement"]'):
        record = parse_event(element)
        if record:
            records.append(record)

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue']): item
        for item in records
    }
    result = sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'], item['title']),
    )
    if not result:
        log_message(
            'No valid Syracuse Friends of Chamber Music concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return result


class SyrfcmOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='syrfcm_org',
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
        return scrape_concerts()


def main():
    SyrfcmOrgCrawler().run()


if __name__ == '__main__':
    main()
