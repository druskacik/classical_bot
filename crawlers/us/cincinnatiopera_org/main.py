import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cincinnatiopera.org/'
SOURCE = 'Cincinnati Opera'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
MONTH_API_URL = urljoin(SOURCE_URL, 'api/open/GetItemsByMonth')
COLLECTION_ID = '579b6569e58c62582a20c122'
START_YEAR = 2016  # The current Squarespace site was created in 2016.
FUTURE_YEARS = 2
MAX_WORKERS = 6
LOCAL_TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(unescape(str(value)), 'html.parser')
    for node in soup.select('script, style, nav, form'):
        node.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_values(now=None):
    now = now or datetime.now(timezone.utc)
    return [
        f'{month:02d}-{year}'
        for year in range(START_YEAR, now.year + FUTURE_YEARS + 1)
        for month in range(1, 13)
    ]


def fetch_month(session, month):
    response = session.get(
        MONTH_API_URL,
        params={'month': month, 'collectionId': COLLECTION_ID},
        timeout=45,
    )
    response.raise_for_status()
    return response.json()


def fetch_detail(session, url):
    last_error = None
    for _attempt in range(2):
        try:
            response = session.get(url, params={'format': 'json'}, timeout=45)
            response.raise_for_status()
            return response.json().get('item') or {}
        except (requests.RequestException, ValueError) as error:
            last_error = error
    raise last_error


def parse_city(location):
    address_line = (location or {}).get('addressLine2') or ''
    city = re.split(r'\s*,\s*', clean_html(address_line), maxsplit=1)[0]
    return city.strip()


def parse_item(item):
    title = clean_html(item.get('title'))
    url = urljoin(SOURCE_URL, item.get('fullUrl') or '')
    location = item.get('location') or {}
    venue = clean_html(location.get('addressTitle'))
    city = parse_city(location)
    start_ms = item.get('startDate')
    if not title or not start_ms or not venue or not city or not url.startswith(SOURCE_URL):
        return None

    try:
        start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).astimezone(LOCAL_TIMEZONE)
    except (TypeError, ValueError, OSError):
        return None

    description_parts = []
    for value in (item.get('excerpt'), item.get('body')):
        text = clean_html(value)
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
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

    items_by_id = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_month, session, month): month
            for month in month_values()
        }
        for future in as_completed(futures):
            month = futures[future]
            try:
                for item in future.result():
                    item_id = item.get('id')
                    if item_id:
                        items_by_id[item_id] = item
            except (requests.RequestException, ValueError, TypeError) as error:
                log_message(
                    'Calendar month request failed',
                    event='crawler_request_failed',
                    level='warning',
                    url=MONTH_API_URL,
                    month=month,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for item in items_by_id.values():
            url = urljoin(SOURCE_URL, item.get('fullUrl') or '')
            if url.startswith(SOURCE_URL):
                futures[executor.submit(fetch_detail, session, url)] = (url, item)

        for future in as_completed(futures):
            url, summary = futures[future]
            try:
                detail = future.result() or summary
            except (requests.RequestException, ValueError, TypeError) as error:
                log_message(
                    'Event detail request failed; using calendar data',
                    event='crawler_request_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                detail = summary
            record = parse_item(detail)
            if record:
                records.append(record)

    if not records:
        log_message(
            'No parseable calendar events found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class CincinnatiOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cincinnatiopera_org',
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
        return scrape_concerts()


def main():
    CincinnatiOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
