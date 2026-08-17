import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bayatlanticsymphony.org/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Bay Atlantic Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name[:3].upper(): number
    for number, name in enumerate(
        [
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ],
        1,
    )
}

DATE_RE = re.compile(
    r'\b(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|'
    r'JUL(?:Y)?|AUG(?:UST)?|SEP(?:TEMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|'
    r'DEC(?:EMBER)?)\s+(\d{1,2})(?:\s*/\s*(\d{1,2}))?'
    r'(?:\s*,?\s*(20\d{2}))?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?\b', re.IGNORECASE)

VENUES = (
    (
        re.compile(r'Rowan College|RCSJ|Cumberland', re.IGNORECASE),
        'Guaracini Performing Arts Center',
        'Vineland',
    ),
    (
        re.compile(r'Stockton(?: University)?(?: Performing Arts Center| PAC)?', re.IGNORECASE),
        'Stockton University Performing Arts Center',
        'Galloway',
    ),
    (re.compile(r'Cape May Convention Hall', re.IGNORECASE), 'Cape May Convention Hall', 'Cape May'),
    (
        re.compile(r'(?:Episcopal )?Church of the Advent', re.IGNORECASE),
        'Church of the Advent',
        'Cape May',
    ),
    (
        re.compile(r'Potena Performing Arts Center', re.IGNORECASE),
        'Potena Performing Arts Center',
        'Margate City',
    ),
    (
        re.compile(r'Avalon Community (?:Center|Hall)', re.IGNORECASE),
        'Avalon Community Hall',
        'Avalon',
    ),
)

SKIP_PATH_PREFIXES = (
    '/calendar-1',
    '/store-1',
    '/videos-1',
    '/melody-and-color',
    '/https/youtube',
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour, minute, period = match.groups()
    hour = int(hour) % 12 + (12 if period.upper() == 'P' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def inferred_year(month, explicit_year, last_modified):
    if explicit_year:
        return int(explicit_year)
    if not last_modified:
        return None
    modified = datetime.strptime(last_modified[:10], '%Y-%m-%d').date()
    # Season pages are normally created or revised close to their performance.
    # A late-year edit can announce the following January-May performance.
    return modified.year + (1 if modified.month >= 8 and month <= 5 else 0)


def valid_date(year, month, day):
    try:
        return datetime(year, month, day).date().isoformat()
    except (TypeError, ValueError):
        return None


def page_text_and_title(html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text((soup.title.string if soup.title and soup.title.string else ''))
    title = re.sub(r'\s+[—|-]\s+Bay Atlantic Symphony\s*$', '', title).strip()
    main = soup.find('main') or soup.body
    if not main:
        return title, ''
    for node in main.select('script, style, nav, footer, .header, .newsletter-block'):
        node.decompose()
    return title, clean_text(main.get_text('\n', strip=True))


def venue_occurrences(text, occurrence_count):
    found = []
    for pattern, venue, city in VENUES:
        match = pattern.search(text)
        if match:
            found.append((match.start(), venue, city))
    found.sort()

    unique = []
    for _, venue, city in found:
        if (venue, city) not in unique:
            unique.append((venue, city))
    if len(unique) == occurrence_count:
        return unique
    if occurrence_count == 1 and len(unique) == 1:
        return unique
    return []


def records_from_page(url, html, last_modified):
    title, text = page_text_and_title(html)
    title = re.sub(r'(?:\s*\(Copy\))+$', '', title, flags=re.IGNORECASE).strip()
    excluded_title = re.search(r'\b(?:video|season (?:overview|at a glance))\b', title, re.IGNORECASE)
    if (
        not title
        or not text
        or excluded_title
        or title.lower() in {
            'bay atlantic symphony', '25-26 season', 'upcoming events 22-23', 'general 1'
        }
    ):
        return []

    match = DATE_RE.search(text)
    if not match:
        return []
    month_name, first_day, second_day, explicit_year = match.groups()
    month = MONTHS[month_name[:3].upper()]
    year = inferred_year(month, explicit_year, last_modified)
    if not year:
        return []

    days = [int(first_day)]
    if second_day:
        days.append(int(second_day))
    dates = [valid_date(year, month, day) for day in days]
    if any(value is None for value in dates):
        return []

    venues = venue_occurrences(text, len(dates))
    if not venues:
        return []

    time_matches = list(TIME_RE.finditer(text))
    times = [parse_time(item.group(0)) for item in time_matches]
    if len(times) != len(dates):
        times = [None] * len(dates)

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': times[index],
            'venue': venues[index][0],
            'city': venues[index][1],
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for index, event_date in enumerate(dates)
    ]


def sitemap_pages(xml):
    soup = BeautifulSoup(xml, 'xml')
    pages = []
    for node in soup.find_all('url'):
        location = clean_text(node.loc.get_text()) if node.loc else ''
        if not location:
            continue
        parsed = urlparse(location)
        if parsed.netloc != 'www.bayatlanticsymphony.org':
            continue
        if parsed.path in {'/', '/sitemap.xml'} or parsed.path.startswith(SKIP_PATH_PREFIXES):
            continue
        modified = clean_text(node.lastmod.get_text()) if node.lastmod else ''
        pages.append((location, modified))
    return pages


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()

    records = []
    for url, last_modified in sitemap_pages(response.text):
        try:
            page = session.get(url, timeout=30)
            page.raise_for_status()
            records.extend(records_from_page(url, page.text, last_modified))
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_page_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No valid concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique.setdefault(key, record)
    return sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['venue']))


class BayAtlanticSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bayatlanticsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    BayAtlanticSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
