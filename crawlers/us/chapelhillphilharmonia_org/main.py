import base64
import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chapelhillphilharmonia.org/'
UPCOMING_URL = urljoin(SOURCE_URL, 'upcoming-events')
ARCHIVE_API_URL = urljoin(SOURCE_URL, '_api/cloud-data/v2/items/query')
ACCESS_TOKENS_URL = urljoin(SOURCE_URL, '_api/v1/access-tokens')
SOURCE = 'Chapel Hill Philharmonia'
VENUE = 'Moeser Auditorium at UNC Chapel Hill'
CITY = 'Chapel Hill'
WIX_DATA_APP = 'e593b0bd-b783-45b8-97c2-873d42aacaf4'
WIX_COLLECTION_APP = '9928bff3-a620-4c06-a434-4af364813209'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
DATE_RE = re.compile(r'^[A-Z]+ \d{1,2}, \d{4}$', re.I)
TIME_RE = re.compile(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$', re.I)


def clean_text(value):
    if not value:
        return ''
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value).title(), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.fullmatch(clean_text(value))
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'pm' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def parse_wix_date(value):
    if not isinstance(value, dict) or not value.get('$date'):
        return None
    try:
        return datetime.fromisoformat(value['$date'].replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        return None


def make_record(title, date, url, description=None, time_from=None):
    if not title or not date:
        return None
    return {
        'title': clean_text(title),
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'US',
        'description': clean_text(description) or None,
    }


def scrape_upcoming(session):
    response = session.get(UPCOMING_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    lines = [clean_text(item) for item in soup.get_text('\n').splitlines()]
    lines = [item for item in lines if item]

    start = next((i for i, item in enumerate(lines) if re.fullmatch(r'20\d{2}-20\d{2} Season', item)), None)
    end = next((i for i, item in enumerate(lines) if item == 'Interested in our past performances?'), len(lines))
    if start is None:
        return []
    lines = lines[start + 1:end]

    records = []
    indexes = [i for i, item in enumerate(lines) if DATE_RE.fullmatch(item)]
    for position, index in enumerate(indexes):
        stop = indexes[position + 1] if position + 1 < len(indexes) else len(lines)
        block = lines[index:stop]
        date = parse_date(block[0])
        if not date or len(block) < 3:
            continue
        time_from = parse_time(block[1]) if block[1].lower() != 'time tbd' else None
        title_index = 2 if time_from or block[1].lower() == 'time tbd' else 1
        title = block[title_index]
        description = '\n'.join(block[title_index + 1:])
        record = make_record(title, date, UPCOMING_URL, description, time_from)
        if record:
            records.append(record)
    return records


def wix_query_token(offset=0, limit=100):
    query = {
        'dataCollectionId': 'Projects',
        'query': {
            'filter': {},
            'sort': [{'fieldName': 'sortDate', 'order': 'DESC'}],
            'paging': {'offset': offset, 'limit': limit},
            'fields': [],
        },
        'referencedItemOptions': [],
        'returnTotalCount': True,
        'environment': 'LIVE',
        'appId': WIX_COLLECTION_APP,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(query, separators=(',', ':')).encode()
    ).decode()
    return encoded.rstrip('=')


def archive_description(data):
    parts = []
    for number in ('One', 'Two', 'Three', 'Four', 'Five'):
        heading = clean_text(data.get(f'info{number}'))
        detail = clean_text(data.get(f'info{number}Description'))
        value = '\n'.join(item for item in (heading, detail) if item)
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def scrape_archives(session):
    token_response = session.get(ACCESS_TOKENS_URL, timeout=45)
    token_response.raise_for_status()
    access_token = token_response.json()['apps'][WIX_DATA_APP]['accessToken']
    response = session.get(
        ARCHIVE_API_URL,
        params={'.r': wix_query_token()},
        headers={'Authorization': access_token, 'x-wix-brand': 'wix'},
        timeout=45,
    )
    response.raise_for_status()

    records = []
    for item in response.json().get('dataItems', []):
        data = item.get('data', {})
        date = parse_date(data.get('date')) or parse_wix_date(data.get('sortDate'))
        path = data.get('link-projects-title')
        if not path:
            continue
        record = make_record(
            data.get('title'),
            date,
            urljoin(SOURCE_URL, path),
            archive_description(data),
        )
        if record:
            records.append(record)
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = scrape_upcoming(session) + scrape_archives(session)
    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class ChapelHillPhilharmoniaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chapelhillphilharmonia_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ChapelHillPhilharmoniaOrgCrawler().run()


if __name__ == '__main__':
    main()
