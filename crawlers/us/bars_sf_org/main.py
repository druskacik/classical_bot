import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://bars-sf.org/'
SOURCE = 'Bay Area Rainbow Symphony'
COLLECTION_URLS = (
    f'{SOURCE_URL}concerts?format=json',
    f'{SOURCE_URL}pastconcerts?format=json',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}
LOCAL_TIMEZONE = ZoneInfo('America/Los_Angeles')


def clean_html(value):
    if not value:
        return ''
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup.select('script, style'):
        element.decompose()
    text = soup.get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_datetime(timestamp):
    if not isinstance(timestamp, (int, float)):
        return None
    value = datetime.fromtimestamp(timestamp / 1000, tz=LOCAL_TIMEZONE)
    return value.date().isoformat(), value.strftime('%H:%M')


def city_from_location(location, description):
    address = ' '.join(
        str(location.get(key) or '') for key in ('addressLine1', 'addressLine2')
    )
    match = re.search(r'\bSan Franc(?:isco|iso)\b', address, re.IGNORECASE)
    if match:
        return 'San Francisco'
    if re.search(r'\bSan Francisco\b', description, re.IGNORECASE):
        return 'San Francisco'
    return None


def venue_from_item(location, description):
    venue = html.unescape(str(location.get('addressTitle') or '')).strip()
    if venue:
        return re.sub(r'\s+', ' ', venue)

    # Some newer Squarespace entries omit the structured location while naming
    # the hall in the event body. These patterns deliberately stop before the
    # street address so that an address is never used as a venue.
    patterns = (
        r'\b(San Francisco Conservatory of Music)\b',
        r'\b(SFCM Hume Hall)\b',
        r'\b(Herbst Theatre)\b',
        r'\b(Taube Atrium (?:Theatre|Theater|Auditorium))\b',
        r"\b(St\. Mark's Lutheran Church)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def parse_item(item):
    title = re.sub(r'\s+', ' ', html.unescape(str(item.get('title') or ''))).strip()
    start = event_datetime(item.get('startDate'))
    path = str(item.get('fullUrl') or '').strip()
    url = requests.compat.urljoin(SOURCE_URL, path)
    description = clean_html(item.get('body'))
    location = item.get('location') if isinstance(item.get('location'), dict) else {}
    venue = venue_from_item(location, description)
    city = city_from_location(location, description)

    if not all((title, start, path, venue, city)):
        return None

    return {
        'title': title,
        'date': start[0],
        'url': url,
        'time_from': start[1],
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BarsSfOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bars_sf_org',
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
        records = []
        seen_ids = set()
        for collection_url in COLLECTION_URLS:
            try:
                response = requests.get(collection_url, headers=HEADERS, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch BARS concert collection',
                    event='crawler_fetch_failed',
                    level='error',
                    url=collection_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            items = [*(payload.get('upcoming') or []), *(payload.get('past') or [])]
            for item in items:
                item_id = item.get('id')
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
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
    BarsSfOrgCrawler().run()


if __name__ == '__main__':
    main()
