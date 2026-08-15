import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://billingssymphony.org/'
SOURCE = 'Billings Symphony'
CITY = 'Billings'
SHOWS_URL = urljoin(SOURCE_URL, 'shows/')
PAST_EVENTS_URL = urljoin(SOURCE_URL, 'past-events/')

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
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def is_detail_url(url):
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split('/') if part]
    return (
        parsed.netloc == urlparse(SOURCE_URL).netloc
        and len(parts) >= 3
        and parts[0] == 'shows'
        and '%' not in parsed.path
    )


def links_matching(soup, base_url, predicate):
    urls = []
    for link in soup.select('a[href]'):
        url = urljoin(base_url, link.get('href')).split('#', 1)[0]
        if predicate(url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def event_json(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get('@type') in {'Event', 'MusicEvent'}:
            return payload
    return None


def parse_date(value):
    value = clean_text(value)
    value = re.sub(r'^[A-Za-z]+\s+', '', value)
    for pattern in ('%b %d, %Y', '%B %d, %Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])M\b', clean_text(value), re.I)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.upper() == 'P' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def occurrences_from_html(soup):
    occurrences = []
    for date_node in soup.select('.show-details .show-date'):
        event_date = parse_date(date_node.get_text(' ', strip=True))
        if not event_date:
            continue
        time_node = date_node.find_next_sibling()
        time_from = None
        if time_node and 'show-time' in (time_node.get('class') or []):
            time_from = parse_time(time_node.get_text(' ', strip=True))
        occurrences.append((event_date, time_from))
    return occurrences


def occurrences_from_json(payload):
    children = payload.get('subEvent') or []
    if not isinstance(children, list):
        children = [children]
    occurrences = []
    for child in children:
        if not isinstance(child, dict):
            continue
        value = child.get('startDate')
        event_date = parse_date(str(value).split('T', 1)[0]) if value else None
        if event_date:
            time_from = parse_time(str(value).split('T', 1)[1]) if 'T' in str(value) else None
            occurrences.append((event_date, time_from))
    if not occurrences:
        value = payload.get('startDate')
        event_date = parse_date(str(value).split('T', 1)[0]) if value else None
        if event_date:
            occurrences.append((event_date, None))
    return occurrences


def parse_detail_page(soup, url):
    payload = event_json(soup)
    if not payload:
        return []

    title = clean_text(payload.get('name'))
    canonical_url = payload.get('url') or payload.get('@id') or url
    location = payload.get('location') or {}
    if not isinstance(location, dict):
        location = {}
    address = location.get('address') or {}
    if not isinstance(address, dict):
        address = {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality')) or CITY
    description = clean_text(payload.get('description')) or None

    if not title or not venue or not city or not str(canonical_url).startswith(('http://', 'https://')):
        return []

    occurrences = occurrences_from_html(soup) or occurrences_from_json(payload)
    records = []
    for event_date, time_from in occurrences:
        records.append({
            'title': title,
            'date': event_date,
            'url': canonical_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class BillingssymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='billingssymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        try:
            current_soup = get_soup(session, SHOWS_URL)
            archive_soup = get_soup(session, PAST_EVENTS_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Billings Symphony event index',
                event='crawler_listing_request_failed',
                level='error',
                url=SHOWS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        detail_urls = links_matching(current_soup, SHOWS_URL, is_detail_url)
        season_urls = links_matching(
            archive_soup,
            PAST_EVENTS_URL,
            lambda value: value.startswith(PAST_EVENTS_URL) and value != PAST_EVENTS_URL,
        )
        for season_url in season_urls:
            try:
                season_soup = get_soup(session, season_url)
                detail_urls.extend(links_matching(season_soup, season_url, is_detail_url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Billings Symphony archive season',
                    event='crawler_archive_request_failed',
                    level='warning',
                    url=season_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        detail_urls = list(dict.fromkeys(detail_urls))
        records = []

        def fetch_detail(detail_url):
            thread_session = make_session()
            try:
                return parse_detail_page(get_soup(thread_session, detail_url), detail_url)
            finally:
                thread_session.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in detail_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Billings Symphony event detail',
                        event='crawler_detail_request_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        if not records:
            log_message(
                'No Billings Symphony events found',
                event='crawler_empty_listing',
                level='warning',
                url=SHOWS_URL,
                record_count=0,
            )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
        )


def main():
    BillingssymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
