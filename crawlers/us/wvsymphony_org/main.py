import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wvsymphony.org/'
SOURCE = 'West Virginia Symphony Orchestra'
CALENDAR_URL = f'{SOURCE_URL}season-calendar'
LOCAL_TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
}

# The Squarespace calendar was created in 2016 and still exposes events from
# its first 2016-17 season. Scan whole calendar months because the collection
# mixes performances with fundraising, media, and other organizational events.
FIRST_YEAR = 2016
FUTURE_YEARS = 2


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def month_values():
    final_year = date.today().year + FUTURE_YEARS
    return [
        f'{month:02d}-{year}'
        for year in range(FIRST_YEAR, final_year + 1)
        for month in range(1, 13)
    ]


def fetch_month(month_value):
    session = make_session()
    try:
        response = session.get(
            CALENDAR_URL,
            params={'view': 'calendar', 'month': month_value, 'format': 'json'},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get('items') or []
    finally:
        session.close()


def description_from_item(item):
    markup = item.get('body') or item.get('excerpt') or ''
    soup = BeautifulSoup(markup, 'html.parser')
    for node in soup.select('script, style, noscript'):
        node.decompose()
    return clean_text(soup)


def city_from_location(location):
    address_line = clean_text(location.get('addressLine2'))
    if not address_line:
        return None
    city = clean_text(address_line.split(',', 1)[0]).strip(' ,')
    return city or None


def datetime_fields(item):
    start_ms = item.get('startDate')
    if not isinstance(start_ms, (int, float)):
        return None
    start = datetime.fromtimestamp(start_ms / 1000, tz=LOCAL_TIMEZONE)
    end_ms = item.get('endDate')
    end = None
    if isinstance(end_ms, (int, float)):
        end = datetime.fromtimestamp(end_ms / 1000, tz=LOCAL_TIMEZONE)
    return start.date().isoformat(), start.strftime('%H:%M'), end.strftime('%H:%M') if end else None


def make_record(item):
    title = clean_text(item.get('title'))
    path = clean_text(item.get('fullUrl'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    date_fields = datetime_fields(item)
    if not title or not path or not venue or not city or not date_fields:
        return None

    event_date, time_from, time_to = date_fields
    url = requests.compat.urljoin(SOURCE_URL, path)
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'time_to': time_to,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description_from_item(item) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class WvSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wvsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'time_to', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        items_by_id = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_month, month_value): month_value
                for month_value in month_values()
            }
            for future in as_completed(futures):
                month_value = futures[future]
                try:
                    items = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch WVSO calendar month',
                        event='crawler_page_failed',
                        level='warning',
                        url=CALENDAR_URL,
                        month=month_value,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                for item in items:
                    item_id = item.get('id') or item.get('fullUrl')
                    if item_id:
                        items_by_id[item_id] = item

        records = []
        for item in items_by_id.values():
            record = make_record(item)
            if record:
                records.append(record)
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    WvSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
