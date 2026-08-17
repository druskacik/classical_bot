import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://promusicamn.com/'
SOURCE = 'ProMusica Minnesota'
CONCERTS_URL = f'{SOURCE_URL}concerts'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

FULL_DATE_RE = re.compile(
    r'\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2}),\s+(?P<year>20\d{2})\b',
    re.IGNORECASE,
)
EVENT_DATE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>20\d{2}))?'
    r'(?:\s+(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?))?$',
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r'^(?P<venue>.+)\.\s*(?P<city>[A-Za-z \'-]+),\s*MN\.?$', re.IGNORECASE
)


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(match):
    if not match.group('hour'):
        return None
    value = f"{match.group('hour')}:{match.group('minute') or '00'} {match.group('ampm')}"
    value = value.replace('.', '')
    try:
        return datetime.strptime(value.upper(), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None


def find_location(soup):
    for heading in soup.select('h2, h3, h4'):
        value = ' '.join(clean_text(heading).split())
        match = LOCATION_RE.match(value)
        if not match:
            continue
        venue = match.group('venue').replace('. ', ', ').strip(' .')
        city = match.group('city').strip(' .')
        if venue and city:
            return venue, city
    return None


def parse_concerts(html):
    soup = BeautifulSoup(html, 'html.parser')
    location = find_location(soup)
    if location is None:
        return []

    year_by_month_day = {}
    for match in FULL_DATE_RE.finditer(clean_text(soup)):
        key = (match.group('month').casefold(), int(match.group('day')))
        year_by_month_day[key] = int(match.group('year'))

    blocks = soup.select('.sqs-html-content')
    event_blocks = []
    for index, block in enumerate(blocks):
        date_heading = block.find(['h2', 'h3', 'h4'], string=EVENT_DATE_RE)
        if date_heading is None:
            continue
        match = EVENT_DATE_RE.match(' '.join(date_heading.get_text(' ', strip=True).split()))
        if match:
            event_blocks.append((index, block, date_heading, match))

    records = []
    venue, city = location
    for position, (index, block, date_heading, match) in enumerate(event_blocks):
        key = (match.group('month').casefold(), int(match.group('day')))
        year = int(match.group('year')) if match.group('year') else year_by_month_day.get(key)
        if year is None:
            continue
        try:
            event_date = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
            ).date().isoformat()
        except ValueError:
            continue

        title_parts = [
            ' '.join(heading.get_text(' ', strip=True).split())
            for heading in block.find_all('h2')
            if heading.find_previous() is not date_heading
        ]
        title = ' '.join(part for part in title_parts if part).replace(': ', ': ')
        title = re.sub(r'\s+', ' ', title).strip()
        if not title:
            continue

        next_index = event_blocks[position + 1][0] if position + 1 < len(event_blocks) else len(blocks)
        description_parts = []
        for detail_block in blocks[index:next_index]:
            text = clean_text(detail_block)
            if 'Memorable Moments from Past Performances' in text:
                break
            description_parts.append(text)
        description = '\n\n'.join(part for part in description_parts if part) or None

        records.append(
            {
                'title': title,
                'date': event_date,
                'url': CONCERTS_URL,
                'time_from': parse_time(match),
                'venue': venue,
                'city': city,
                'description': description,
            }
        )
    return records


class PromusicamnComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='promusicamn_com',
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
        try:
            response = make_session().get(CONCERTS_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch ProMusica Minnesota concerts page',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return parse_concerts(response.text)


def main():
    PromusicamnComCrawler().run()


if __name__ == '__main__':
    main()
