import re
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chambermusicsociety.org/'
SOURCE = 'Chamber Music Society of Lincoln Center'
API_URL = 'https://d1cs1sx7k4kk4i.cloudfront.net/Prod/event-feed/5/live'

# These first-party calendar types contain performances or performance-based
# educational events. Plain lectures are deliberately excluded.
INCLUDED_TYPES = {'Concert', 'Family Events', 'Inside Chamber Music', 'Master Class'}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

CANADIAN_PROVINCES = {
    'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT'
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def tour_location(title):
    match = re.fullmatch(r'(.+?),\s*([A-Z]{2})', clean_text(title))
    if not match:
        return None
    city, region = match.groups()
    country_code = 'CA' if region in CANADIAN_PROVINCES else 'US'
    return city, country_code


def program_text(programs):
    lines = []
    for program in programs or []:
        composer = clean_text((program.get('Composer') or {}).get('Title'))
        work = clean_text(program.get('Title'))
        composed = clean_text(program.get('Composed'))
        if not composer and not work:
            continue
        line = ' — '.join(part for part in (composer, work) if part)
        if composed:
            line += f' ({composed})'
        lines.append(line)
    return lines


def is_candidate(item):
    performance_type = item.get('PerformanceType')
    if performance_type in INCLUDED_TYPES:
        return True
    # Some first-party concert records, especially tour performances, have no
    # type. A concrete published programme is strong evidence of performance.
    return performance_type is None and bool(item.get('Programs'))


def item_to_record(item):
    parsed_datetime = parse_datetime(item.get('PerformanceDate'))
    venue = clean_text(item.get('Venue') or item.get('Facility'))
    event_link = item.get('EventLink')
    if not parsed_datetime or not venue or not event_link:
        return None

    if item.get('IsTour'):
        location = tour_location(item.get('Title'))
        title = clean_text(item.get('Suffix') or item.get('Prefix'))
        if not location or not title:
            return None
        city, country_code = location
    else:
        title = clean_text(item.get('Title'))
        city, country_code = 'New York', 'US'

    url = urljoin(SOURCE_URL, event_link)
    if not title or not url.startswith(('http://', 'https://')):
        return None

    description_parts = []
    details = clean_text(item.get('Details'))
    if details:
        description_parts.append(details)
    programmes = program_text(item.get('Programs'))
    if programmes:
        description_parts.append('Programme:\n' + '\n'.join(programmes))

    event_date, time_from = parsed_datetime
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(API_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError('CMS event feed did not return a list')

    records = []
    for item in payload:
        if not isinstance(item, dict) or not is_candidate(item):
            continue
        record = item_to_record(item)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No candidate performances found in CMS event feed',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ChamberMusicSocietyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusicsociety_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ChamberMusicSocietyOrgCrawler().run()


if __name__ == '__main__':
    main()
