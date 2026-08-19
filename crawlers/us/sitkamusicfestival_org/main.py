import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sitkamusicfestival.org/'
SOURCE = 'Sitka Music Festival'
JSON_URL = f'{SOURCE_URL}?format=json'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
TIMEZONE = ZoneInfo('America/Anchorage')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_city(location):
    address = clean_text((location or {}).get('addressLine2'))
    if not address:
        return ''
    # Squarespace stores these as either "Sitka, Alaska 99835" or
    # "Ketchikan, Alaska, 99901".
    city = re.split(r',\s*(?:Alaska|AK)\b', address, maxsplit=1, flags=re.I)[0]
    return clean_text(city)


def event_record(item):
    title = clean_text(item.get('title'))
    path = item.get('fullUrl')
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = parse_city(location)
    start_ms = item.get('startDate')
    if not title or not path or not venue or not city or not isinstance(start_ms, (int, float)):
        return None

    try:
        start = datetime.fromtimestamp(start_ms / 1000, tz=TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return None

    description = clean_text(item.get('body') or item.get('excerpt')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_events(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    url = JSON_URL
    visited_pages = set()
    seen_events = set()
    records = []

    # The collection's timestamp pagination occasionally overlaps page edges.
    # Keep its past/upcoming payloads, then use the first-party sitemap to find
    # any published event detail pages omitted at those boundaries.
    sitemap_response = session.get(SITEMAP_URL, timeout=45)
    sitemap_response.raise_for_status()
    sitemap = BeautifulSoup(sitemap_response.text, 'xml')
    sitemap_urls = {
        node.get_text(strip=True)
        for node in sitemap.find_all('loc')
        if '/events/' in node.get_text(strip=True)
    }

    while url and url not in visited_pages:
        visited_pages.add(url)
        response = session.get(url, timeout=45)
        response.raise_for_status()
        payload = response.json()

        for item in payload.get('upcoming', []) + payload.get('past', []):
            event_key = (item.get('id'), item.get('startDate'))
            if event_key in seen_events:
                continue
            seen_events.add(event_key)
            record = event_record(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipping event without required fields',
                    event='crawler_record_skipped',
                    level='warning',
                    url=urljoin(SOURCE_URL, item.get('fullUrl') or ''),
                    error_type='missing_required_field',
                )

        next_path = (payload.get('pagination') or {}).get('nextPageUrl')
        url = urljoin(SOURCE_URL, next_path) + '&format=json' if next_path else None

    scraped_urls = {record['url'] for record in records}
    for detail_url in sorted(sitemap_urls - scraped_urls):
        response = session.get(f'{detail_url}?format=json', timeout=45)
        response.raise_for_status()
        item = response.json().get('item') or {}
        event_key = (item.get('id'), item.get('startDate'))
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        record = event_record(item)
        if record:
            records.append(record)

    if not records:
        log_message(
            'No events found',
            event='crawler_empty_listing',
            level='warning',
            url=JSON_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class SitkaMusicFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sitkamusicfestival_org',
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
            'source_url',
            'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return scrape_events()


def main():
    SitkaMusicFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
