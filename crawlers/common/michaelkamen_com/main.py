import html
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.michaelkamen.com/'
CATALOGUE_URL = urljoin(SOURCE_URL, 'catalogue?format=page-context')
SOURCE = 'Michael Kamen - Official Website'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

# Catalogue entries are historical and use prose rather than event fields. Only
# locations explicitly named by the site are mapped; entries without one of
# these defensible venue/city pairs are skipped.
LOCATIONS = (
    (re.compile(r'Royal Albert Hall', re.I), 'Royal Albert Hall', 'London', 'GB'),
    (re.compile(r'Old Vic Theatre', re.I), 'Old Vic Theatre', 'London', 'GB'),
    (
        re.compile(r'John F\. Kennedy Center for the Performing Arts', re.I),
        'John F. Kennedy Center for the Performing Arts',
        'Washington, D.C.',
        'US',
    ),
    (re.compile(r'Parco Novi Sad', re.I), 'Parco Novi Sad', 'Modena', 'IT'),
    (re.compile(r'Todai-ji Temple', re.I), 'Todai-ji Temple', 'Nara', 'JP'),
)

DATE_PATTERN = re.compile(
    r'(?P<start>\d{1,2})(?:st|nd|rd|th)?'
    r'(?:\s*[-–]\s*(?P<end>\d{1,2})(?:st|nd|rd|th)?)?'
    r'\s+(?P<month>' + '|'.join(MONTHS) + r')\s+(?P<year>19\d{2}|20\d{2})',
    re.I,
)
MONTH_FIRST_DATE_PATTERN = re.compile(
    r'(?P<month>' + '|'.join(MONTHS) + r')\s+'
    r'(?P<start>\d{1,2})(?:st|nd|rd|th)?[,]?\s+'
    r'(?P<year>19\d{2}|20\d{2})',
    re.I,
)


def clean_text(value):
    text = BeautifulSoup(value or '', 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def dates_from_match(match):
    dates = []
    start = int(match.group('start'))
    end = int(match.groupdict().get('end') or start)
    if end < start or end - start > 7:
        return []
    month = MONTHS[match.group('month').lower()]
    year = int(match.group('year'))
    for day in range(start, end + 1):
        try:
            dates.append(date(year, month, day).isoformat())
        except ValueError:
            continue
    return dates


def dated_locations(text):
    date_matches = [
        *DATE_PATTERN.finditer(text),
        *MONTH_FIRST_DATE_PATTERN.finditer(text),
    ]
    locations = []
    for pattern, venue, city, country_code in LOCATIONS:
        locations.extend(
            (match, venue, city, country_code) for match in pattern.finditer(text)
        )

    results = []
    for match in date_matches:
        nearby = min(
            locations,
            key=lambda location: abs(location[0].start() - match.start()),
            default=None,
        )
        # Dates elsewhere in an album's liner notes can describe unrelated
        # studio sessions, so require the venue and date to share a sentence-
        # sized span of text.
        if nearby is None or abs(nearby[0].start() - match.start()) > 220:
            continue
        _, venue, city, country_code = nearby
        for event_date in dates_from_match(match):
            results.append((event_date, venue, city, country_code))
    return list(dict.fromkeys(results))


def parse_dates(text):
    dates = []
    for match in DATE_PATTERN.finditer(text):
        dates.extend(dates_from_match(match))
    return list(dict.fromkeys(dates))


def parse_location(text):
    for pattern, venue, city, country_code in LOCATIONS:
        if pattern.search(text):
            return venue, city, country_code
    return None


def item_records(item):
    if 'Concerts' not in item.get('categories', []):
        return []

    title = clean_text(item.get('title'))
    description = clean_text(item.get('body'))
    full_url = item.get('fullUrl')
    occurrences = dated_locations(description)
    if not title or not description or not full_url or not occurrences:
        return []

    url = urljoin(SOURCE_URL, full_url)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, venue, city, country_code in occurrences
    ]


def get_page(session, offset=None):
    params = {'format': 'page-context'}
    if offset is not None:
        params['offset'] = offset
    response = session.get(urljoin(SOURCE_URL, 'catalogue'), params=params, timeout=45)
    response.raise_for_status()
    return response.json()


class MichaelKamenComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='michaelkamen_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        offset = None

        while True:
            try:
                page = get_page(session, offset)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Michael Kamen catalogue',
                    event='crawler_fetch_failed',
                    level='error',
                    url=CATALOGUE_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for item in page.get('items', []):
                records.extend(item_records(item))

            pagination = page.get('pagination') or {}
            if not pagination.get('nextPage'):
                break
            next_offset = pagination.get('nextPageOffset')
            if next_offset is None or next_offset == offset:
                raise ValueError('Catalogue pagination did not provide a new offset')
            offset = next_offset

        return sorted(
            records,
            key=lambda record: (record['date'], record['title'], record['venue']),
        )


def main():
    MichaelKamenComCrawler().run()


if __name__ == '__main__':
    main()
