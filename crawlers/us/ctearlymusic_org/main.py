import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ctearlymusic.org/'
SOURCE = 'Connecticut Early Music Festival'
TICKET_COLLECTIONS = ('tickets', 'tickets-for-bravura-baroque')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month.lower(): number
    for number, month in enumerate(
        (
            '', 'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        )
    ) if month
}

# The store usually puts the date in its product title. This separately listed
# product publishes its date only in the first-party product image.
KNOWN_PRODUCT_DATES = {'/tickets-for-bravura-baroque/bravura-baroque': '2026-04-19'}

VENUES = {
    'chester meeting house': ('Chester Meeting House', 'Chester', 'US'),
    'harkness chapel': ('Harkness Chapel, Connecticut College', 'New London', 'US'),
    'st. ann': ("St. Ann's Parish", 'Old Lyme', 'US'),
    'saint ann': ("St. Ann's Parish", 'Old Lyme', 'US'),
    'red barn at mitchell college': ('Red Barn at Mitchell College', 'New London', 'US'),
    'st. john': ("St. John's Episcopal Church", 'Niantic', 'US'),
    'saint john': ("St. John's Episcopal Church", 'Niantic', 'US'),
    'evans hall': ('Evans Hall, Connecticut College', 'New London', 'US'),
    'noank baptist church': ('Noank Baptist Church', 'Groton', 'US'),
    'george kent performance hall': ('George Kent Performance Hall', 'Westerly', 'US'),
    'la grua center': ('La Grua Center', 'Stonington', 'US'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, url):
    response = session.get(url, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    return response.json()


def resolve_venue(text):
    normalized = text.lower().replace('’', "'")
    for marker, location in VENUES.items():
        if marker in normalized:
            return location
    return None


def valid_date(year, month, day):
    try:
        return date(int(year), MONTHS[month.lower()], int(day)).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def parse_time(text):
    match = re.search(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?M\.?', text, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'P' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def collection_year(payload):
    text = ' '.join(
        str(payload.get('collection', {}).get(key) or '')
        for key in ('title', 'navigationTitle', 'description')
    )
    match = re.search(r'\b(20\d{2})\b', text)
    return int(match.group(1)) if match else None


def product_record(item, year):
    path = item.get('fullUrl') or ''
    title = clean_text(item.get('title'))
    description = clean_text(item.get('excerpt'))
    if not path or not description:
        return None

    event_date = KNOWN_PRODUCT_DATES.get(path)
    match = re.search(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})',
        title,
        re.I,
    )
    if not event_date and match and year:
        event_date = valid_date(year, match.group(1), match.group(2))
    location = resolve_venue(description)
    if not event_date or not location:
        return None

    venue, city, country_code = location
    if not title:
        title = item.get('urlId', '').replace('-', ' ').title()
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, path),
        'time_from': parse_time(description),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def archive_record(payload, url):
    description = clean_text(payload.get('mainContent'))
    heading = clean_text(payload.get('collection', {}).get('title'))
    match = re.search(
        r'\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b',
        f'{description}\n{heading}',
        re.I,
    )
    location = resolve_venue(description)
    if not match or not location:
        return None
    event_date = valid_date(match.group(3), match.group(2), match.group(1))
    if not event_date:
        return None

    lines = [line.strip(' ;') for line in description.splitlines() if line.strip()]
    date_line = next((index for index, line in enumerate(lines) if re.search(
        r'\b\d{1,2}\s+[A-Za-z]+\s+20\d{2}\b', line, re.I
    )), None)
    venue, city, country_code = location
    venue_index = next((index for index, line in enumerate(lines) if resolve_venue(line)), None)
    title_lines = lines[(date_line + 1 if date_line is not None else 0):venue_index]
    title = ' – '.join(title_lines[:2])
    if not title:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(lines[date_line]) if date_line is not None else None,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def archive_urls(session):
    response = session.get(urljoin(SOURCE_URL, 'sitemap.xml'), timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    pattern = re.compile(r'/\d{1,2}-[a-z]+-20\d{2}-content/?$', re.I)
    return sorted({tag.get_text(strip=True) for tag in soup.select('loc') if pattern.search(tag.get_text())})


class CtEarlyMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ctearlymusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []

        for collection in TICKET_COLLECTIONS:
            url = urljoin(SOURCE_URL, collection)
            try:
                payload = get_json(session, url)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch ticket collection', event='crawler_fetch_failed',
                    level='error', url=url, error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise
            year = collection_year(payload)
            for item in payload.get('items') or []:
                record = product_record(item, year)
                if record:
                    records.append(record)

        try:
            urls = archive_urls(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch sitemap archive', event='crawler_fetch_failed',
                level='error', url=urljoin(SOURCE_URL, 'sitemap.xml'),
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        for url in urls:
            try:
                record = archive_record(get_json(session, url), url)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch archived concert', event='crawler_item_failed',
                    level='warning', url=url, error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ))


def main():
    CtEarlyMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
