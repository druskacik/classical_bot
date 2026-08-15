import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://baychamber.org/'
SOURCE = 'Bay Chamber'
CALENDAR_URL = f'{SOURCE_URL}calendar'
TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_html(value):
    if not isinstance(value, str) or not value.strip():
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup.select('script, style, noscript'):
        element.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def parse_city(location):
    address_line = str(location.get('addressLine2') or '').strip()
    if not address_line:
        return None
    city = address_line.split(',', 1)[0].strip()
    return city or None


def parse_event(item):
    if not isinstance(item, dict):
        return None

    title = html.unescape(str(item.get('title') or '')).strip()
    url_id = str(item.get('urlId') or '').strip().strip('/')
    start_timestamp = item.get('startDate')
    location = item.get('location') if isinstance(item.get('location'), dict) else {}
    venue = html.unescape(str(location.get('addressTitle') or '')).strip()
    city = parse_city(location)
    country_name = str(location.get('addressCountry') or '').strip().lower()

    if not isinstance(start_timestamp, (int, float)):
        return None
    try:
        start = datetime.fromtimestamp(start_timestamp / 1000, tz=TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return None

    country_code = 'US' if country_name in {'united states', 'united states of america', 'us', 'usa'} else None
    if not all((title, url_id, venue, city, country_code)):
        return None

    description = clean_html(item.get('body')) or clean_html(item.get('excerpt'))
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': f'{CALENDAR_URL}/{url_id}',
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BayChamberOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='baychamber_org',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        records = []
        seen_ids = set()
        offset = None

        while True:
            params = {'format': 'json'}
            if offset is not None:
                params['offset'] = offset
            try:
                response = requests.get(CALENDAR_URL, params=params, headers=HEADERS, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Bay Chamber calendar page',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if 'response' in locals() else CALENDAR_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for item in [*(payload.get('upcoming') or []), *(payload.get('past') or [])]:
                item_id = str(item.get('id') or '')
                if item_id and item_id in seen_ids:
                    continue
                record = parse_event(item)
                if record:
                    records.append(record)
                if item_id:
                    seen_ids.add(item_id)

            pagination = payload.get('pagination') or {}
            next_offset = pagination.get('nextPageOffset') if pagination.get('nextPage') else None
            if next_offset is None or next_offset == offset:
                break
            offset = next_offset

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    BayChamberOrgCrawler().run()


if __name__ == '__main__':
    main()
