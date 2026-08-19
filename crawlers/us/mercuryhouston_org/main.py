import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mercuryhouston.org/'
SOURCE = 'Mercury Chamber Orchestra'
EVENTS_URL = urljoin(SOURCE_URL, 'events/')
CALENDAR_URL = urljoin(EVENTS_URL, 'calendar/')
API_URL = (
    'https://tix.mercuryhouston.org/mercurychamberorchestra/api/v3'
)

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
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = str(value).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def event_slug(title):
    value = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode()
    value = value.lower().replace('&', ' ')
    return re.sub(r'[^a-z0-9]+', '-', value).strip('-')


def instance_url(title, start):
    parsed = datetime.fromisoformat(start)
    hour = parsed.strftime('%I').lstrip('0') or '0'
    suffix = parsed.strftime('%p').lower()
    slug = event_slug(title)
    return urljoin(
        EVENTS_URL,
        f'{slug}-{parsed:%Y-%m-%d}-{hour}{parsed:%M}-{suffix}',
    )


def get_json(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        if isinstance(value, dict) and isinstance(value.get('@graph'), list):
            candidates.extend(value['@graph'])
        for candidate in candidates:
            kind = candidate.get('@type') if isinstance(candidate, dict) else None
            if kind == 'Event' or isinstance(kind, list) and 'Event' in kind:
                return candidate
    return None


def page_description(soup):
    parts = []
    for selector in (
        '.mpspx-event-single-infobox-inner',
        '.mpspx-event-single-custom1-inner',
    ):
        text = clean_text(soup.select_one(selector))
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event_page(session, url, fallback_title=None, fallback_start=None):
    response = session.get(url, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    schema = event_schema(soup)
    if not schema:
        return None

    title = clean_text(schema.get('name')) or clean_text(fallback_title)
    start = schema.get('startDate') or fallback_start
    location = schema.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country = clean_text(address.get('addressCountry'))
    if isinstance(schema.get('location'), list):
        return None
    try:
        parsed = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    if not title or not venue or not city:
        return None

    canonical = response.url.rstrip('/')
    return {
        'title': title,
        'date': parsed.date().isoformat(),
        'url': canonical,
        'time_from': parsed.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country.upper() if len(country) == 2 else 'US',
        'description': page_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def page_links(session, url, selector):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return {
        urljoin(response.url, node['href']).split('#', 1)[0]
        for node in soup.select(selector)
        if node.get('href')
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    session.mount(
        'https://',
        HTTPAdapter(
            pool_connections=12,
            pool_maxsize=12,
            max_retries=Retry(
                total=2,
                connect=2,
                read=2,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=('GET',),
            ),
        ),
    )
    events = get_json(session, f'{API_URL}/events')
    records = []
    seen_urls = set()
    current_tasks = []

    for event in events:
        if clean_text(event.get('attribute_Series')).lower() == 'virtual':
            continue
        instances_url = f"{API_URL}/events/{event['id']}/instances"
        try:
            instances = get_json(session, instances_url)
        except requests.RequestException as error:
            log_message(
                'Event instances request failed',
                event='crawler_request_failed',
                level='warning',
                url=instances_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for instance in instances:
            url = instance_url(event['name'], instance['start'])
            current_tasks.append((url, event['name'], instance['start']))

    def fetch_current(task):
        url, title, start = task
        try:
            return url, parse_event_page(
                session,
                url,
                fallback_title=title,
                fallback_start=start,
            ), None
        except requests.RequestException as error:
            return url, None, error

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_current, task) for task in current_tasks]
        for future in as_completed(futures):
            url, record, error = future.result()
            if error:
                log_message(
                    'Event detail request failed',
                    event='crawler_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
                seen_urls.add(record['url'].rstrip('/'))

    # The calendar contains unticketed performances (for example free outdoor
    # concerts) that are not present in the Spektrix API.
    extra_urls = page_links(session, CALENDAR_URL, 'a[href*="/events/"]')
    pending_urls = []
    for url in sorted(extra_urls):
        url = url.rstrip('/')
        if url in seen_urls or url in {EVENTS_URL.rstrip('/'), CALENDAR_URL.rstrip('/')}:
            continue
        if '(virtual)' in url.lower() or '-virtual-' in url.lower():
            continue
        pending_urls.append(url)

    def fetch_extra(url):
        try:
            return url, parse_event_page(session, url), None
        except requests.RequestException as error:
            return url, None, error

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_extra, url) for url in pending_urls]
        for future in as_completed(futures):
            url, record, error = future.result()
            if error:
                log_message(
                    'Calendar event detail request failed',
                    event='crawler_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record and '(virtual)' not in record['title'].lower():
                records.append(record)
                seen_urls.add(record['url'].rstrip('/'))

    if not records:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class MercuryHoustonOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mercuryhouston_org',
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
    MercuryHoustonOrgCrawler().run()


if __name__ == '__main__':
    main()
