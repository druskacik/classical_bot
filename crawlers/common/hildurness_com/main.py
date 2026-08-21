import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hildurness.com/'
SOURCE = 'Hildur Guðnadóttir'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'JAN': 1,
    'FEB': 2,
    'MAR': 3,
    'APR': 4,
    'MAY': 5,
    'JUN': 6,
    'JUL': 7,
    'AUG': 8,
    'SEPT': 9,
    'SEP': 9,
    'OCT': 10,
    'NOV': 11,
    'DEC': 12,
}

# The tour page has no machine-readable addresses. Only locations explicitly
# named in a listing are accepted; unknown locations are skipped safely.
LOCATIONS = {
    'Warsaw': ('Avant Art Festival', 'PL'),
    'Rome': ('Romaeuropa', 'IT'),
    'Oslo': ('Den Norske Opera', 'NO'),
    'Berlin': ('Philharmonie Berlin', 'DE'),
    'Antwerp': ('DeSingel', 'BE'),
    'Brussels': ('Bozar', 'BE'),
    'Trenčín': ('Pohoda Festival', 'SK'),
    'Amsterdam': ('Holland Festival', 'NL'),
    'Reykjavik': ("Harpa's Silfurberg", 'IS'),
    'London': ('Barbican', 'GB'),
}

DATE_RE = re.compile(
    r'^\s*(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)'
    r'\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?,\s*(20\d{2})\s*[-—–]\s*',
    re.IGNORECASE,
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_dates(text):
    match = DATE_RE.match(text)
    if not match:
        return [], text

    month = MONTHS[match.group(1).upper()]
    first_day = int(match.group(2))
    last_day = int(match.group(3) or first_day)
    year = int(match.group(4))

    # Long spans on this page are festival/residency overview listings rather
    # than individual performances and must not become placeholder events.
    if last_day < first_day or last_day - first_day > 2:
        return [], text[match.end():].strip()

    try:
        first = date(year, month, first_day)
        last = date(year, month, last_day)
    except ValueError:
        return [], text[match.end():].strip()

    dates = []
    current = first
    while current <= last:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates, text[match.end():].strip()


def parse_location(text):
    folded = text.casefold()
    for city, (default_venue, country_code) in LOCATIONS.items():
        if city.casefold() not in folded:
            continue

        venue = default_venue
        before_city = re.split(re.escape(city), text, maxsplit=1, flags=re.IGNORECASE)[0]
        candidate = before_city.rstrip(' ,')
        if ',' in candidate:
            candidate = candidate.rsplit(',', 1)[-1].strip()
        if candidate and len(candidate) <= 80:
            venue = candidate

        # Festival labels occasionally follow the city; they are not venues.
        if venue.casefold() in {'reykjavik arts festival', 'philharmonie'}:
            venue = default_venue
        return venue, city, country_code
    return None


def parse_listing(paragraph):
    full_text = clean_text(paragraph)
    dates, remainder = parse_dates(full_text)
    location = parse_location(remainder)
    if not dates or not location:
        return []

    venue, city, country_code = location
    link = paragraph.find('a', href=True)
    url = link['href'].strip() if link else SOURCE_URL

    location_text = clean_text(link) if link else f'{venue}, {city}'
    title = remainder
    if location_text and title.endswith(location_text):
        title = title[:-len(location_text)].rstrip(' ,')
    if not title:
        title = f'{SOURCE} — {venue}'

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': full_text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
    ]


class HildurnessComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hildurness_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        dedupe_subset=['title', 'date', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Hildur Guðnadóttir live listings',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        heading = next(
            (item for item in soup.find_all(['h2', 'h3', 'h4'])
             if clean_text(item).upper() == 'LIVE TOUR'),
            None,
        )
        if heading is None:
            raise ValueError('Could not find the LIVE TOUR section')

        records = []
        for paragraph in heading.find_all_next('p'):
            if paragraph.find_previous(['h2', 'h3', 'h4']) != heading:
                break
            records.extend(parse_listing(paragraph))

        return sorted(
            records,
            key=lambda record: (record['date'], record['title'], record['venue']),
        )


def main():
    HildurnessComCrawler().run()


if __name__ == '__main__':
    main()
