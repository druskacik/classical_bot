import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mcovt.org/'
SOURCE = 'Montpelier Chamber Orchestra'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
LEGACY_PROGRAMS_URL = urljoin(SOURCE_URL, 'programs')
ARCHIVE_2023_URL = urljoin(SOURCE_URL, '20232024-archive')
TIME_ZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    if '<' in str(value):
        value = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def get_json(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def collection_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    urls = {urljoin(SOURCE_URL, '20262027-season')}

    # Event detail URLs in Squarespace's sitemap include a dated path below
    # their collection. Deriving the parent keeps this working after the
    # navigation moves on to a newly named season collection.
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        path = urlparse(url).path.rstrip('/')
        match = re.match(r'(.+?)/\d{4}/\d{1,2}/\d{1,2}/[^/]+$', path)
        if match:
            urls.add(urljoin(SOURCE_URL, match.group(1).lstrip('/')))
    return sorted(urls)


def event_items(session, collection_url):
    offset = 0
    seen = set()
    while True:
        payload = get_json(
            session,
            collection_url,
            params={'format': 'json', 'offset': offset} if offset else {'format': 'json'},
        )
        collection = payload.get('collection') or {}
        if collection.get('typeName') != 'events':
            return
        items = (payload.get('upcoming') or []) + (payload.get('past') or [])
        fresh = [item for item in items if item.get('id') not in seen]
        if not fresh:
            return
        yield from fresh
        seen.update(item.get('id') for item in fresh)
        page_size = int(collection.get('pageSize') or 30)
        offset += page_size
        if offset >= int(collection.get('itemCount') or len(seen)):
            return


def city_from_location(location):
    address = clean_text(' '.join([
        location.get('addressLine1') or '',
        location.get('addressLine2') or '',
    ]))
    match = re.search(r'([^,\n]+),\s*VT\b', address, re.I)
    return clean_text(match.group(1)) if match else ''


def record_from_item(item):
    title = clean_text(item.get('title'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    start_ms = item.get('startDate')
    full_url = item.get('fullUrl')
    if not all([title, venue, city, start_ms, full_url]):
        return None

    try:
        start = datetime.fromtimestamp(float(start_ms) / 1000, TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None

    description_parts = [clean_text(item.get('body')), clean_text(item.get('excerpt'))]
    description = '\n\n'.join(part for part in description_parts if part) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
    }


def page_text(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    main = soup.select_one('main') or soup.body or soup
    return clean_text(main.get_text('\n', strip=True))


def legacy_records(session):
    programs = page_text(session, LEGACY_PROGRAMS_URL)
    archive = page_text(session, ARCHIVE_2023_URL)
    programs_lower = programs.lower()
    records = []

    legacy_programs = [
        ('Classically Now', '2019-11-16', '19:00', 'FALL PROGRAM: CLASSICALLY NOW', 'HOLIDAY PROGRAM:'),
        ('Classically Now', '2019-11-17', '15:00', 'FALL PROGRAM: CLASSICALLY NOW', 'HOLIDAY PROGRAM:'),
        ('Amahl & The Night Visitors', '2020-01-04', '19:00', 'HOLIDAY PROGRAM: AMAHL & THE NIGHT VISITORS', 'SPRING PROGRAM:'),
        ('Amahl & The Night Visitors', '2020-01-05', '14:00', 'HOLIDAY PROGRAM: AMAHL & THE NIGHT VISITORS', 'SPRING PROGRAM:'),
    ]
    for title, date, time_from, start_label, end_label in legacy_programs:
        start = programs_lower.find(start_label.lower())
        end = programs_lower.find(end_label.lower(), start + 1)
        if start < 0 or end < 0:
            continue
        records.append({
            'title': title,
            'date': date,
            'url': LEGACY_PROGRAMS_URL,
            'time_from': time_from,
            'venue': 'City Hall Arts Center (Lost Nation Theater)',
            'city': 'Montpelier',
            'country_code': 'US',
            'description': programs[start:end].strip() or None,
        })

    fall_start = archive.find('2023 FALL Program:')
    nye_start = archive.find("2023 New Year’s Eve:")
    if fall_start >= 0 and nye_start > fall_start:
        records.append({
            'title': 'Music for a Vibrant City',
            'date': '2023-11-18',
            'url': ARCHIVE_2023_URL,
            'time_from': '19:00',
            'venue': 'Barre Opera House',
            'city': 'Barre',
            'country_code': 'US',
            'description': archive[fall_start:nye_start].strip() or None,
        })
        records.append({
            'title': 'First Night Friends',
            'date': '2023-12-31',
            'url': ARCHIVE_2023_URL,
            'time_from': None,
            'venue': 'Unitarian Church',
            'city': 'Montpelier',
            'country_code': 'US',
            'description': archive[nye_start:].strip() or None,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in collection_urls(session):
        try:
            for item in event_items(session, url):
                record = record_from_item(item)
                if record:
                    records.append(record)
        except (requests.RequestException, ValueError, TypeError) as error:
            log_message(
                'Unable to scrape event collection',
                event='crawler_collection_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    try:
        records.extend(legacy_records(session))
    except requests.RequestException as error:
        log_message(
            'Unable to scrape legacy concert pages',
            event='crawler_legacy_failed',
            level='warning',
            url=SOURCE_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    if not records:
        log_message(
            'No valid concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class McovtOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mcovt_org',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    McovtOrgCrawler().run()


if __name__ == '__main__':
    main()
