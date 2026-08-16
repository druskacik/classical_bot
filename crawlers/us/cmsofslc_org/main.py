import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cmsofslc.org/'
SOURCE = 'Chamber Music Society of Salt Lake City'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
LOCAL_TIMEZONE = ZoneInfo('America/Denver')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup.select('script, style, noscript'):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def local_datetime(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, tz=LOCAL_TIMEZONE)


def parse_city(location):
    address = html.unescape(location.get('addressLine2') or '')
    match = re.match(r'\s*([^,]+)', address)
    return match.group(1).strip() if match else None


def parse_item(item):
    title = html.unescape(item.get('title') or '').strip()
    full_url = item.get('fullUrl') or item.get('urlId')
    location = item.get('location') or {}
    venue = html.unescape(location.get('addressTitle') or '').strip()
    city = parse_city(location)
    start_timestamp = item.get('startDate')

    if not title or not full_url or not venue or not city or not start_timestamp:
        return None

    try:
        start = local_datetime(start_timestamp)
        end = local_datetime(item['endDate']) if item.get('endDate') else None
    except (OSError, OverflowError, TypeError, ValueError):
        return None

    description = clean_text(item.get('body')) or None
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(CALENDAR_URL + '/', full_url),
        'time_from': start.strftime('%H:%M'),
        'time_to': end.strftime('%H:%M') if end else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
    }


class CmsofslcOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cmsofslc_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'time_to',
            'venue',
            'city',
            'country_code',
            'description',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def _fetch_feed(self, session, category=None):
        params = {'format': 'json'}
        if category:
            params['category'] = category
        try:
            response = session.get(CALENDAR_URL, params=params, timeout=45)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch CMS of Salt Lake City calendar feed',
                event='crawler_fetch_failed',
                level='error',
                url=response.url if 'response' in locals() else CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        initial_feed = self._fetch_feed(session)
        feeds = [initial_feed]
        # Squarespace limits the combined event view. Its first-party season
        # categories provide stable archive slices and avoid losing old events.
        for category in initial_feed.get('collection', {}).get('categories', []):
            feeds.append(self._fetch_feed(session, category=category))

        items_by_key = {}
        for feed in feeds:
            for item in feed.get('upcoming', []) + feed.get('past', []):
                key = (item.get('fullUrl') or item.get('urlId'), item.get('startDate'))
                items_by_key[key] = item

        records = []
        for item in items_by_key.values():
            record = parse_item(item)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    CmsofslcOrgCrawler().run()


if __name__ == '__main__':
    main()
