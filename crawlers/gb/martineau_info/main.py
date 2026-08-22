import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://martineau.info/'
CALENDAR_URL = 'https://martineau.info/upcoming-calendar/'
SOURCE = 'Malcolm Martineau'

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

# The calendar is for a UK-based artist but contains touring engagements.  Its
# location labels are free text, so only well-established place names are used;
# an unfamiliar location is skipped rather than assigned to the wrong country.
LOCATIONS = {
    'barcelona': ('Barcelona', 'ES'),
    'chipping campden': ('Chipping Campden', 'GB'),
    'chipping camden': ('Chipping Campden', 'GB'),
    'eppan': ('Eppan', 'IT'),
    'hohenems': ('Hohenems', 'AT'),
    'honehems': ('Hohenems', 'AT'),
    'london': ('London', 'GB'),
    'madrid': ('Madrid', 'ES'),
    'newport': ('Newport', 'US'),
    'st. michael': ('Eppan', 'IT'),
    'toronto': ('Toronto', 'CA'),
    'vienna': ('Vienna', 'AT'),
    'zeist': ('Zeist', 'NL'),
}


def clean_text(element):
    if element is None:
        return ''
    return re.sub(r'\s+', ' ', element.get_text(' ', strip=True)).strip()


def parse_date(value):
    match = re.search(
        r'\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b',
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_location(value):
    normalized = value.casefold()
    for marker, location in LOCATIONS.items():
        if marker in normalized:
            return location

    # The festival name is used without its city in this calendar.
    if 'campden' in normalized or 'camden music festival' in normalized:
        return 'Chipping Campden', 'GB'
    if 'schubertiade' in normalized:
        return 'Hohenems', 'AT'
    if 'toronto summer music' in normalized:
        return 'Toronto', 'CA'
    return None


def parse_row(row):
    cells = row.find_all(['td', 'th'], recursive=False)
    if len(cells) < 3 or clean_text(cells[0]).casefold() == 'date':
        return None

    date_link = cells[0].find('a', href=True)
    event_date = parse_date(clean_text(cells[0]))
    venue = clean_text(cells[1])
    title = clean_text(cells[2])
    location = parse_location(venue)

    # Rows announcing a future festival schedule are overview records, not a
    # concrete performance occurrence.
    if 'schedule and line-up' in title.casefold():
        return None
    if not date_link or not event_date or not venue or not title or not location:
        return None

    city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(CALENDAR_URL, date_link['href']),
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MartineauInfoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='martineau_info',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
        try:
            response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Malcolm Martineau calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for row in soup.select('article table tr'):
            record = parse_row(row)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MartineauInfoCrawler().run()


if __name__ == '__main__':
    main()
