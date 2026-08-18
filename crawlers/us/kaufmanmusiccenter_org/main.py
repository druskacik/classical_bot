import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kaufmanmusiccenter.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kc/calendar/')
SOURCE = 'Kaufman Music Center'
FIRST_ARCHIVE_YEAR = 2018
FUTURE_MONTHS = 18
MAX_WORKERS = 3

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml',
}


def clean_text(value):
    if value is None:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r' *\n+ *', '\n', text).strip()


def canonical_url(value):
    url = urljoin(SOURCE_URL, value)
    parts = urlsplit(url)
    return urlunsplit(('https', 'www.kaufmanmusiccenter.org', parts.path, '', ''))


def month_urls(now=None):
    now = now or datetime.now(timezone.utc)
    last_index = now.year * 12 + now.month - 1 + FUTURE_MONTHS
    for year in range(FIRST_ARCHIVE_YEAR, last_index // 12 + 1):
        first_month = 1
        final_month = 12
        if year == last_index // 12:
            final_month = last_index % 12 + 1
        for month in range(first_month, final_month + 1):
            yield urljoin(CALENDAR_URL, f'{year}/{month:02d}/')


def event_urls_from_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = set()
    for link in soup.select('#calendar a.entry[href*="/event/"]'):
        urls.add(canonical_url(link.get('href')))
    return urls


def event_data_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        raw = unescape(script.string or script.get_text())
        try:
            payload = json.loads(raw, strict=False)
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if isinstance(item, dict) and item.get('@type') in {'MusicEvent', 'Event'}:
                return item
    return None


def city_from_location(location):
    address = location.get('address') if isinstance(location, dict) else None
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
        country = clean_text(address.get('addressCountry')).upper()
        if city and country in {'', 'US', 'USA', 'UNITED STATES'}:
            return city
        return None

    address = clean_text(address)
    if re.search(r'\bNew York\s*,\s*NY\b', address, re.I):
        return 'New York'
    return None


def record_from_html(html, url):
    data = event_data_from_html(html)
    if not data:
        return None

    title = clean_text(data.get('name'))
    location = data.get('location') or {}
    venue = clean_text(location.get('name')) if isinstance(location, dict) else ''
    city = city_from_location(location)
    start = clean_text(data.get('startDate'))
    if not title or not venue or not city or not start:
        return None

    try:
        parsed = datetime.fromisoformat(start.replace('Z', '+00:00'))
    except ValueError:
        return None

    return {
        'title': title,
        'date': parsed.date().isoformat(),
        'url': canonical_url(url),
        'time_from': parsed.strftime('%H:%M') if 'T' in start else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(data.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_html(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry, pool_maxsize=MAX_WORKERS))
    event_urls = set()

    for url in month_urls():
        try:
            event_urls.update(event_urls_from_calendar(fetch_html(session, url)))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Kaufman Music Center calendar month',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    records = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_html, session, url): url for url in event_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = record_from_html(future.result(), url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Kaufman Music Center event',
                    event='crawler_event_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    unique = {
        (item['title'], item['date'], item['time_from'], item['venue'], item['url']): item
        for item in records
    }
    result = sorted(unique.values(), key=lambda item: (
        item['date'], item['time_from'] or '', item['title'], item['venue']
    ))
    if not result:
        log_message(
            'No valid Kaufman Music Center events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return result


class KaufmanMusicCenterOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kaufmanmusiccenter_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'url'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    KaufmanMusicCenterOrgCrawler().run()


if __name__ == '__main__':
    main()
