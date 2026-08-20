import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.texarkanasymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}pages-sitemap.xml'
SOURCE = 'Texarkana Symphony Orchestra'
CITY = 'Texarkana'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'^(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),?\s+'
    r'([A-Z]+\s+\d{1,2},\s+\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[AP]M)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', value).strip()


def parse_date(value):
    match = DATE_RE.match(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).title(), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = clean_text(value).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def extract_venue(lines, date_index):
    nearby = lines[date_index:date_index + 5]
    for line in nearby:
        if 'PEROT THEATRE' in line.upper():
            return 'Perot Theatre'
    for line in nearby[1:]:
        upper = line.upper()
        if 'AUDITORIUM' in upper and not TIME_RE.fullmatch(line):
            return clean_text(line.lstrip('I ')).title()
    return None


def extract_times(lines, date_index):
    # The first line after the date is the advertised performance-time line.
    # Later times can be concert previews and must not become occurrences.
    time_line = lines[date_index]
    if not TIME_RE.search(time_line) and date_index + 1 < len(lines):
        time_line = lines[date_index + 1]
    values = []
    for match in TIME_RE.findall(time_line):
        parsed = parse_time(match)
        if parsed and parsed not in values:
            values.append(parsed)
    return values or [None]


def parse_event_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    lines = [clean_text(line) for line in soup.get_text('\n').splitlines()]
    lines = [line for line in lines if line]

    date_index = next((index for index, line in enumerate(lines) if DATE_RE.match(line)), None)
    if date_index is None:
        return []
    event_date = parse_date(lines[date_index])
    venue = extract_venue(lines, date_index)
    if not event_date or not venue:
        return []

    page_title = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
    title = re.sub(r'\s*\|\s*TSO\s*$', '', page_title, flags=re.IGNORECASE).strip()
    if not title:
        return []

    description_lines = lines[date_index + 1:]
    description_lines = [line for line in description_lines if not line.startswith('©')]
    description = '\n'.join(description_lines).strip() or None

    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for time_from in extract_times(lines, date_index)]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.content, 'xml')
    urls = [clean_text(node.get_text()) for node in sitemap.find_all('loc')]

    records = []
    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_event_page(url, response.text))
        except requests.RequestException as error:
            log_message(
                'Unable to fetch event page',
                event='crawler_page_fetch_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concert detail pages found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class TexarkanaSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='texarkanasymphony_org',
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
    TexarkanaSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
