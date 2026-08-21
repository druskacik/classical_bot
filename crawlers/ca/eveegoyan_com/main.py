import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://eveegoyan.com/'
SOURCE = 'Eve Egoyan'
UPCOMING_API = f'{SOURCE_URL}wp-json/wp/v2/pages/254'
PAST_API = f'{SOURCE_URL}wp-json/wp/v2/pages/8'
UPCOMING_URL = f'{SOURCE_URL}upcoming-performances/upcoming/'
PAST_URL = f'{SOURCE_URL}upcoming-performances/eve-egoyan-recent-performances/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

# The site is a hand-written archive rather than an event database. These
# first-party names are the reliable location evidence in its recent entries.
LOCATIONS = (
    ('Maison de la culture du Plateau-Mont-Royal', 'Montréal', 'CA'),
    ('Satosphère', 'Montréal', 'CA'),
    ('Cornell University', 'Ithaca', 'US'),
    ('Burdock Music Hall', 'Toronto', 'CA'),
    ('Walter Hall', 'Toronto', 'CA'),
    ('8EAST', 'Toronto', 'CA'),
    ('George Weston Recital Hall', 'Toronto', 'CA'),
    ('The Array Space', 'Toronto', 'CA'),
    ('Array Space', 'Toronto', 'CA'),
    ('Roundhouse', 'Vancouver', 'CA'),
    ('Sala Rosa', 'Montréal', 'CA'),
    ('Komitas Museum-Institute', 'Yerevan', 'AM'),
    ('Yerevan Komitas State Conservatory', 'Yerevan', 'AM'),
    ('GTC', 'Gyumri', 'AM'),
)

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            '', 'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        )
    )
}
DATE_RE = re.compile(
    r'\b(' + '|'.join(name for name in MONTHS if name) + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)|\b([01]?\d|2[0-3]):([0-5]\d)\b',
    re.IGNORECASE,
)


def clean_text(value):
    value = str(value or '')
    text = BeautifulSoup(value, 'html.parser').get_text(' ', strip=True) if '<' in value else value
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    if match.group(4):
        return f'{int(match.group(4)):02d}:{match.group(5)}'
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if match.group(3).lower().startswith('p') and hour != 12:
        hour += 12
    if match.group(3).lower().startswith('a') and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_date(text, year):
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime(year, MONTHS[match.group(1).title()], int(match.group(2))).date().isoformat()
    except ValueError:
        return None


def resolve_location(text):
    folded = text.casefold()
    for venue, city, country_code in LOCATIONS:
        if venue.casefold() in folded:
            return venue, city, country_code

    # Recent entries sometimes give a city plus the presenting series rather
    # than a building. Retain only named organizations that function as the
    # advertised venue/presenter; never substitute the city for the venue.
    if 'music on main' in folded and 'vancouver' in folded:
        return 'Music on Main', 'Vancouver', 'CA'
    if 'eckhardt-gramatté national music competition' in folded and 'brandon' in folded:
        return 'Eckhardt-Gramatté National Music Competition', 'Brandon', 'CA'
    return None


def title_from_text(text, venue):
    value = DATE_RE.sub('', text, count=1)
    value = re.sub(r'^\s*[,–—-]+\s*', '', value)
    # Prefer an explicitly named programme or concert phrase.
    for pattern in (
        r'(Eve Egoyan:\s*Longing and Belonging)',
        r'(Longing and Belonging)',
        r'(Piano Fest 2026)',
        r'(SMCQ Music and Images Festival)',
        r'(Improvised music at 8EAST)',
        r'(In Stone)',
    ):
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return f'Eve Egoyan at {venue}'


def record_from_text(text, year, url):
    text = clean_text(text)
    match = DATE_RE.search(text)
    date_tail = text[match.end():match.end() + 20] if match else ''
    # A date range does not identify which day contains the public performance.
    if re.match(r'\s*(?:[-–—]\s*\d|and\s+\d)', date_tail, re.IGNORECASE):
        return None
    date = parse_date(text, year)
    location = resolve_location(text)
    if not date or not location:
        return None
    venue, city, country_code = location
    return {
        'title': title_from_text(text, venue),
        'date': date,
        'url': url,
        'time_from': parse_time(text),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': text,
    }


def archive_entries(html):
    soup = BeautifulSoup(html, 'html.parser')
    year = None
    for node in soup.find_all(True):
        candidate = clean_text(node) if node.name in ('strong', 'b', 'p', 'h2', 'h3') else ''
        if re.fullmatch(r'19\d{2}|20\d{2}', candidate) and not node.find_parent('li'):
            year = int(candidate)
        elif node.name == 'li' and year and not node.find_parent('li'):
            yield year, clean_text(node)


def upcoming_entries(html, modified):
    soup = BeautifulSoup(html, 'html.parser')
    text = clean_text(soup)
    year = datetime.fromisoformat(modified).year
    # The page uses bold date/venue markers rather than list items. Split at
    # each date marker while retaining its following programme text.
    markers = list(DATE_RE.finditer(text))
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        yield year, text[marker.start():end]


def fetch_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


class EveEgoyanComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eveegoyan_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        pages = ((UPCOMING_API, UPCOMING_URL, True), (PAST_API, PAST_URL, False))
        for api_url, page_url, is_upcoming in pages:
            try:
                page = fetch_page(session, api_url)
            except (requests.RequestException, ValueError, KeyError) as error:
                log_message(
                    'Failed to fetch concert page',
                    event='crawler_page_failed',
                    level='warning',
                    url=api_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            html = page.get('content', {}).get('rendered', '')
            entries = (
                upcoming_entries(html, page['modified'])
                if is_upcoming else archive_entries(html)
            )
            for year, text in entries:
                record = record_from_text(text, year, page_url)
                if record:
                    records.append(record)

        unique = {}
        for record in records:
            key = (record['title'], record['date'], record['time_from'], record['venue'])
            unique[key] = record
        return sorted(
            unique.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    EveEgoyanComCrawler().run()


if __name__ == '__main__':
    main()
