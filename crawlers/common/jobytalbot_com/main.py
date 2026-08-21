import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jobytalbot.com/'
SOURCE = 'Joby Talbot'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}

# The calendar is international and supplies addresses rather than structured
# location fields. These markers cover its currently published locations. An
# unfamiliar location is deliberately skipped instead of guessed.
LOCATIONS = (
    ('bloomington', 'Bloomington', 'US'),
    ('stuttgart', 'Stuttgart', 'DE'),
    ('buenos aires', 'Buenos Aires', 'AR'),
    ('milan', 'Milan', 'IT'),
    ('bristol', 'Bristol', 'GB'),
    ('london', 'London', 'GB'),
    ('stockholm', 'Stockholm', 'SE'),
    ('berkeley', 'Berkeley', 'US'),
    ('new york', 'New York', 'US'),
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(date_text, event_url):
    match = re.match(
        r'^(\d{1,2})(?:\s*-\s*\d{1,2})?\s+([A-Za-z]+)(?:\s+(20\d{2}))?',
        date_text,
    )
    if not match:
        return None

    month = MONTHS.get(match.group(2).lower())
    if month is None:
        return None

    year = int(match.group(3)) if match.group(3) else None
    if year is None:
        url_years = list(map(int, re.findall(r'(?<!\d)(20[2-9]\d)(?!\d)', event_url)))
        # Season URLs such as /season/2025-2026/... identify a performance in
        # the latter year for spring dates and the former for autumn dates.
        if len(url_years) >= 2 and max(url_years) - min(url_years) == 1:
            year = max(url_years) if month <= 7 else min(url_years)
        else:
            year = url_years[0] if url_years else date.today().year

    try:
        return date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(value):
    normalized = value.casefold()
    for marker, city, country_code in LOCATIONS:
        if marker in normalized:
            venue = value.split(',', 1)[0].strip()
            if venue and venue.casefold() != city.casefold():
                return venue, city, country_code
    return None


def parse_card(block):
    paragraphs = block.select(':scope > p')
    if len(paragraphs) < 4:
        return None

    date_text = clean_text(paragraphs[0])
    title = clean_text(paragraphs[1])
    if not re.match(r'^\d{1,2}', date_text) or not title:
        return None

    more_info = None
    more_info_index = None
    for index, paragraph in enumerate(paragraphs[2:], start=2):
        for link in paragraph.select('a[href]'):
            if clean_text(link).casefold() == 'more info':
                more_info = urljoin(CALENDAR_URL, link['href'])
                more_info_index = index
                break
        if more_info:
            break

    # Product releases and other announcements have no event detail link and
    # no usable performance location on this calendar.
    if not more_info or more_info_index is None or more_info_index < 3:
        return None

    details = [clean_text(paragraph) for paragraph in paragraphs[2:more_info_index]]
    details = [value for value in details if value]
    if not details:
        return None

    location = parse_location(details[-1])
    event_date = parse_date(date_text, more_info)
    if not location or not event_date:
        return None

    venue, city, country_code = location
    description_parts = [title, *details]
    return {
        'title': title,
        'date': event_date,
        'url': more_info,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n'.join(description_parts),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class JobyTalbotComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jobytalbot_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        try:
            response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Joby Talbot calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for block in soup.select('.sqs-html-content'):
            record = parse_card(block)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    JobyTalbotComCrawler().run()


if __name__ == '__main__':
    main()
