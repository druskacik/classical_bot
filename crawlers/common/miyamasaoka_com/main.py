import calendar
import re
from datetime import date, timedelta
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = 'Miya Masaoka'
SOURCE_URL = 'https://miyamasaoka.com/'
CALENDAR_URL = f'{SOURCE_URL}calendar/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

MONTHS = {
    name.casefold(): number
    for number, name in enumerate(calendar.month_name)
    if name
}
MONTH_PATTERN = '|'.join(calendar.month_name[1:])

# The calendar is an international touring archive.  These rules deliberately
# require venue-level evidence and skip entries which only name a city or an
# institution in descriptive prose.
LOCATION_RULES = [
    (r'\bThe Lab\b', 'The Lab', 'San Francisco', 'US'),
    (r'Theater for the New City', 'Theater for the New City', 'New York', 'US'),
    (r'CCRMA Stage|CCRMA LIVE', 'CCRMA Stage', 'Stanford', 'US'),
    (r'Theater [Ii]m Delphi', 'Theater im Delphi', 'Berlin', 'DE'),
    (r'Lichtenber(?:g|s)chule', 'Lichtenbergschule', 'Darmstadt', 'DE'),
    (r'American Academy (?:of|in) Rome|Via Angelo Masina', 'American Academy in Rome', 'Rome', 'IT'),
    (r'Zurich University of the Arts|Academy ZHDK', 'Zurich University of the Arts', 'Zurich', 'CH'),
    (r'Herbst Theatre', 'Herbst Theatre', 'San Francisco', 'US'),
    (r'Pierre Boulez Saal', 'Pierre Boulez Saal', 'Berlin', 'DE'),
    (r'Miller Theater', 'Miller Theatre', 'New York', 'US'),
    (r'Centrum Sztuki Włączającej|Teatr 21', 'Teatr 21', 'Warsaw', 'PL'),
    (r'Mattatoio La Pelanda', 'Mattatoio La Pelanda', 'Rome', 'IT'),
    (r'\bEMPAC\b', 'EMPAC', 'Troy', 'US'),
    (r'Mark Morris Dance Center', 'Mark Morris Dance Center', 'Brooklyn', 'US'),
    (r'Clemente Soto Vélez', 'The Clemente Soto Vélez Cultural & Educational Center', 'New York', 'US'),
    (r'Yamaha Artists Services', 'Yamaha Artists Services', 'New York', 'US'),
    (r'Triple Hall Karolina', 'Triple Hall Karolina', 'Ostrava', 'CZ'),
    (r'DiMenna Center', 'The DiMenna Center for Classical Music', 'New York', 'US'),
    (r'Toronto Biennale,? Main Exhibition', 'Toronto Biennial Main Exhibition Space', 'Toronto', 'CA'),
    (r'Silent Green', 'Silent Green', 'Berlin', 'DE'),
    (r'Wattis Institute|360 Kansas St', 'Wattis Institute for Contemporary Art', 'San Francisco', 'US'),
]


def clean_text(value: str) -> str:
    value = unescape(value).replace('\xa0', ' ')
    value = value.replace('\u2013', '-').replace('\u2014', '-')
    value = re.sub(r'[ \t]+', ' ', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_dates(text: str, url: str) -> list[str]:
    """Return explicit occurrence dates, expanding short same-month runs."""
    year_match = re.search(r'\b(20\d{2})\b', text)
    if not year_match:
        year_match = re.search(r'/(20\d{2})/', url)
    if not year_match:
        return []
    year = int(year_match.group(1))

    match = re.search(
        rf'\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?'
        rf'(?:\s*-\s*(?:(?:{MONTH_PATTERN})\.?\s+)?(\d{{1,2}})(?:st|nd|rd|th)?)?'
        rf'(?:\s*,?\s*(20\d{{2}}))?',
        text,
        re.I,
    )
    if not match:
        return []

    month = MONTHS[match.group(1).casefold()]
    start_day = int(match.group(2))
    end_day = int(match.group(3) or start_day)
    if match.group(4):
        year = int(match.group(4))
    try:
        start = date(year, month, start_day)
        end = date(year, month, end_day)
    except ValueError:
        return []
    if end < start or (end - start).days > 7:
        return [start.isoformat()]
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]


def parse_time(text: str) -> str | None:
    match = re.search(
        r'(?<!\d)([01]?\d|2[0-3])\s*(?::|\.)\s*([0-5]\d)\s*(am|pm)?\b'
        r'|(?<!\d)(1[0-2]|[1-9])\s*(am|pm)\b',
        text,
        re.I,
    )
    if not match:
        return None
    hour = int(match.group(1) or match.group(4))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or match.group(5) or '').casefold()
    # Values such as "1:15" are ambiguous on this English-language calendar;
    # retaining them as 01:15 would be worse than omitting the optional time.
    if not suffix and hour <= 12:
        return None
    if suffix == 'pm' and hour < 12:
        hour += 12
    elif suffix == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_location(text: str):
    flattened = re.sub(r'\s+', ' ', text)
    for pattern, venue, city, country_code in LOCATION_RULES:
        if re.search(pattern, flattened, re.I):
            return venue, city, country_code
    return None


def records_from_html(html: bytes) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('main article li.grid-item'):
        heading = item.select_one('h4 a[href]')
        if heading is None:
            continue
        title = clean_text(heading.get_text(' ', strip=True))
        url = heading.get('href', '').strip()
        description = clean_text(item.get_text('\n', strip=True))
        if description.startswith(title):
            description = description[len(title):].strip()
        dates = parse_dates(description, url)
        location = parse_location(f'{title}\n{description}')
        if not title or not url or not dates or not location:
            continue
        venue, city, country_code = location
        time_from = parse_time(description)
        for event_date in dates:
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class MiyamasaokaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='miyamasaoka_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        log_message('Fetching Miya Masaoka calendar', event='crawler_url_fetch', url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=60)
        response.raise_for_status()
        records = records_from_html(response.content)
        if not records:
            log_message(
                'No parseable Miya Masaoka calendar events found',
                event='crawler_empty_listing',
                level='warning',
                url=CALENDAR_URL,
                record_count=0,
            )
        return records


def main():
    MiyamasaokaComCrawler().run()


if __name__ == '__main__':
    main()
