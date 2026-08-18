import html
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chambermusicdetroit.org/'
SOURCE = 'Chamber Music Detroit'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
TIME_ZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(str(value), 'html.parser')
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', html.unescape(text)).strip()


def sitemap_paths(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    paths = set()
    for node in soup.find_all('loc'):
        parsed = urlsplit(clean_text(node))
        if parsed.netloc.lower() == 'www.chambermusicdetroit.org':
            paths.add(parsed.path.rstrip('/') or '/')
    return paths


def collection_items(session, path, child_paths):
    url = urljoin(SOURCE_URL, path.lstrip('/'))
    response = session.get(url, params={'format': 'json'}, timeout=45)
    response.raise_for_status()
    payload = response.json()
    collection = payload.get('collection') or {}
    if collection.get('typeName') != 'events':
        return None

    items = {}
    for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
        if item.get('fullUrl'):
            items[item['fullUrl'].rstrip('/')] = item

    # Squarespace calendar responses can be page-sized. The sitemap is the
    # authoritative inventory, so fetch any published detail omitted there.
    for child_path in child_paths:
        if child_path in items:
            continue
        detail_url = urljoin(SOURCE_URL, child_path.lstrip('/'))
        try:
            detail_response = session.get(detail_url, params={'format': 'json'}, timeout=45)
            detail_response.raise_for_status()
            item = detail_response.json().get('item') or {}
            if item.get('startDate') and item.get('fullUrl'):
                items[child_path] = item
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Could not fetch event detail',
                event='crawler_detail_failed',
                level='warning',
                url=detail_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return list(items.values())


def city_from_location(location):
    address_line = clean_text((location or {}).get('addressLine2'))
    if not address_line:
        return ''
    return address_line.split(',', 1)[0].strip()


def item_to_record(item):
    title = clean_text(item.get('title'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    full_url = item.get('fullUrl')
    start_timestamp = item.get('startDate')
    if not title or not venue or not city or not full_url or not start_timestamp:
        return None

    try:
        starts_at = datetime.fromtimestamp(float(start_timestamp) / 1000, TIME_ZONE)
    except (TypeError, ValueError, OSError):
        return None

    description_parts = []
    for value in (item.get('excerpt'), item.get('body')):
        text = clean_text(value)
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': starts_at.date().isoformat(),
        'url': urljoin(SOURCE_URL, full_url.lstrip('/')),
        'time_from': starts_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    paths = sitemap_paths(session)
    collection_paths = sorted(
        path for path in paths if any(other.startswith(f'{path}/') for other in paths)
    )

    all_items = {}
    for path in collection_paths:
        child_paths = sorted(other for other in paths if other.startswith(f'{path}/'))
        try:
            items = collection_items(session, path, child_paths)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Could not fetch collection',
                event='crawler_collection_failed',
                level='warning',
                url=urljoin(SOURCE_URL, path.lstrip('/')),
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if items is None:
            continue
        for item in items:
            if item.get('fullUrl'):
                all_items[item['fullUrl'].rstrip('/')] = item

    records = [item_to_record(item) for item in all_items.values()]
    records = [record for record in records if record]
    if not records:
        log_message(
            'No concerts found in published event collections',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ChamberMusicDetroitOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusicdetroit_org',
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
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ChamberMusicDetroitOrgCrawler().run()


if __name__ == '__main__':
    main()
