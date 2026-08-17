import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://shoalssymphony.org/'
SEASON_URL = f'{SOURCE_URL}explore-the-season/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages/168'
SOURCE = 'Shoals Symphony Orchestra at UNA'
CITY = 'Florence'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*[-–—]?\s*'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),\s+(\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(\d{1,2}(?::\d{2})?)\s*([AP])\.?\s*M\.?', re.IGNORECASE)
VENUE_RE = re.compile(r'Location:\s*([^\n\[]+)', re.IGNORECASE)
ROW_RE = re.compile(r'\[et_pb_row\b.*?(?=\[et_pb_row\b|\[/et_pb_section\])', re.DOTALL)
TITLE_RE = re.compile(r'\[et_pb_blurb\b[^]]*\btitle=["“”](.*?)["“”]\s+url=', re.DOTALL)


def clean_text(value):
    if not value:
        return ''
    value = html.unescape(str(value)).replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', value).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime.strptime(f'{month} {day} {year}', '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    clock, meridiem = match.groups()
    value = f'{clock} {meridiem}M'
    try:
        return datetime.strptime(value, '%I:%M %p').strftime('%H:%M')
    except ValueError:
        try:
            return datetime.strptime(value, '%I %p').strftime('%H:%M')
        except ValueError:
            return None


def parse_row(row):
    title_match = TITLE_RE.search(row)
    event_date = parse_date(row)
    venue_match = VENUE_RE.search(BeautifulSoup(row, 'html.parser').get_text('\n', strip=True))
    if not title_match or not event_date or not venue_match:
        return None

    title = clean_text(title_match.group(1))
    venue = clean_text(venue_match.group(1))
    if not title or not venue:
        return None

    description_parts = []
    blurb_end = row.find('[/et_pb_blurb]', title_match.end())
    description_html = row[title_match.end():blurb_end] if blurb_end >= 0 else ''
    soup = BeautifulSoup(description_html, 'html.parser')
    for paragraph in soup.select('p'):
        text = clean_text(paragraph.get_text(' ', strip=True))
        if (
            text
            and not DATE_RE.search(text)
            and not TIME_RE.search(text)
            and not text.lower().startswith(('location:', 'concert duration:'))
            and text not in description_parts
        ):
            description_parts.append(text)

    return {
        'title': title,
        'date': event_date,
        'url': SEASON_URL,
        'time_from': parse_time(row),
        'venue': venue,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(API_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    content = response.json().get('content', {}).get('rendered', '')

    records = []
    for row in ROW_RE.findall(html.unescape(content)):
        record = parse_row(row)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No season concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ShoalsSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='shoalssymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
    ShoalsSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
