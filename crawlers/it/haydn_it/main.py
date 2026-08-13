import json
import re
from datetime import date
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.haydn.it/'
EVENTS_URL = urljoin(SOURCE_URL, 'eventi')
AJAX_URL = urljoin(SOURCE_URL, 'wp-admin/admin-ajax.php')
SOURCE = 'Fondazione Haydn'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = BeautifulSoup(unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', value).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET', 'POST'),
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def listing_payload(offset, page_size):
    return {
        'action': 'getElementsAjax',
        'args[type]': 'filter' if offset == 0 else 'more',
        'args[lang]': 'it',
        'args[query][post_type]': 'event',
        'args[query][posts_per_page]': str(page_size),
        'args[query][offset]': str(offset),
        'args[query][meta_query_relation]': 'AND',
        'args[query][meta_query][0][key]': 'event_date_start',
        'args[query][meta_query][0][value]': '20000101',
        'args[query][meta_query][0][compare]': '>=',
        'args[query][meta_query][1][key]': 'event_date_start',
        'args[query][meta_query][1][value]': '21001231',
        'args[query][meta_query][1][compare]': '<=',
    }


def event_urls(session):
    page_size = 100
    offset = 0
    urls = []
    found = None
    while found is None or offset < found:
        response = session.post(AJAX_URL, data=listing_payload(offset, page_size), timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        items = soup.select('.item')
        if not items:
            break
        if found is None:
            value = items[0].get('data-found-posts')
            found = int(value) if value and value.isdigit() else None
        for link in soup.select('a[href*="/eventi/"]'):
            url = urljoin(SOURCE_URL, link.get('href', '')).split('#', 1)[0]
            parsed = urlparse(url)
            path = parsed.path.rstrip('/')
            if parsed.netloc == 'www.haydn.it' and path.count('/') == 2 and url not in urls:
                urls.append(url)
        offset += len(items)
        if len(items) < page_size:
            break
    return urls


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
    return None


def parse_time(soup):
    value = clean_text(soup.select_one('.hour-start'))
    if not value:
        value = clean_text(soup.select_one('.date-address-container .date'))
    match = re.search(r'\b([01]?\d|2[0-3])[.:]([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_detail(soup, url):
    data = event_schema(soup)
    if not data:
        return None
    location = data.get('location') or {}
    address = location.get('address') or {}
    title = clean_text(data.get('name'))
    event_date = clean_text(data.get('startDate'))[:10]
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    country_code = clean_text(address.get('addressCountry')).upper()
    try:
        date.fromisoformat(event_date)
    except ValueError:
        return None
    if not title or not venue or not city or not re.fullmatch(r'[A-Z]{2}', country_code):
        return None

    description_parts = [clean_text(data.get('description'))]
    works = []
    for work in data.get('workPerformed') or []:
        if not isinstance(work, dict):
            continue
        author = clean_text(work.get('author'))
        name = clean_text(work.get('name'))
        if author and name:
            works.append(f'{author}: {name}')
        elif author or name:
            works.append(author or name)
    if works:
        description_parts.append('Programma:\n' + '\n'.join(works))

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(soup),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(part for part in description_parts if part) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HaydnItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='haydn_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        try:
            urls = event_urls(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Fondazione Haydn event listing',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_detail(BeautifulSoup(response.content, 'html.parser'), url)
                if record:
                    records.append(record)
            except (requests.RequestException, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse Fondazione Haydn event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    HaydnItCrawler().run()


if __name__ == '__main__':
    main()
