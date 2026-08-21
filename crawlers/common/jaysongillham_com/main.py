import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jaysongillham.com/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'schedule')
SOURCE = 'Jayson Gillham'
SITE_TIMEZONE = ZoneInfo('Europe/London')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-GB,en;q=0.9',
}

COUNTRY_CODES = {
    'Australia': 'AU',
    'Italy': 'IT',
    'Portugal': 'PT',
    'South Africa': 'ZA',
    'United Kingdom': 'GB',
}

# Squarespace stores these Australian venues under their suburb rather than
# the municipality normally expected in the city field.
CITY_OVERRIDES = {
    ('AU', 'Newtown'): 'Geelong',
    ('AU', 'South Brisbane'): 'Brisbane',
    ('AU', 'Southbank'): 'Melbourne',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def local_datetime(milliseconds):
    if not isinstance(milliseconds, (int, float)):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, SITE_TIMEZONE)


def parse_location(item):
    location = item.get('location') or {}
    country_code = COUNTRY_CODES.get(clean_text(location.get('addressCountry')))
    address_line_2 = clean_text(location.get('addressLine2'))
    city = address_line_2.split(',', 1)[0].strip()
    city = CITY_OVERRIDES.get((country_code, city), city)

    venue = clean_text(location.get('addressTitle'))
    if not venue and city == 'London':
        # This event is explicitly billed as a recital at this named property.
        venue = clean_text(location.get('addressLine1'))

    if not country_code or not city or not venue:
        return None
    # A region alone is not a defensible city. This affects an old event at
    # Castello della Paneretta whose source record only says "Toscana".
    if city in {'Toscana'}:
        return None
    return venue, city, country_code


def parse_item(item):
    title = clean_text(item.get('title'))
    path = clean_text(item.get('fullUrl'))
    start = local_datetime(item.get('startDate'))
    location = parse_location(item)
    if not title or not path or start is None or location is None:
        return None

    venue, city, country_code = location
    description_parts = [clean_text(item.get('body')), clean_text(item.get('excerpt'))]
    description = '\n\n'.join(
        part for index, part in enumerate(description_parts)
        if part and part not in description_parts[:index]
    ) or None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, path),
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class JaysonGillhamComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jaysongillham_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items_by_id = {}
        categories = [None]

        # The Squarespace unfiltered offset pages can overlap at their
        # boundaries. Unioning them with each category feed closes observed
        # gaps while retaining uncategorized records from the complete feed.
        for category in categories:
            offset = None
            seen_offsets = set()
            while True:
                params = {'format': 'json'}
                if category:
                    params['category'] = category
                if offset is not None:
                    params['offset'] = offset
                try:
                    response = session.get(SCHEDULE_URL, params=params, timeout=45)
                    response.raise_for_status()
                    payload = response.json()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to fetch Jayson Gillham schedule',
                        event='crawler_fetch_failed',
                        level='error',
                        url=response.url if 'response' in locals() else SCHEDULE_URL,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    raise

                if category is None and offset is None:
                    categories.extend(payload.get('collection', {}).get('categories') or [])
                for item in (payload.get('upcoming') or []) + (payload.get('past') or []):
                    item_id = clean_text(item.get('id'))
                    if item_id:
                        items_by_id[item_id] = item

                pagination = payload.get('pagination') or {}
                if not pagination.get('nextPage'):
                    break
                next_offset = pagination.get('nextPageOffset')
                if next_offset is None or next_offset in seen_offsets:
                    raise ValueError('Schedule pagination did not provide a new offset')
                seen_offsets.add(next_offset)
                offset = next_offset

        records = []
        for item in items_by_id.values():
            record = parse_item(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Jayson Gillham schedule item',
                    event='crawler_item_skipped',
                    level='warning',
                    url=urljoin(SOURCE_URL, clean_text(item.get('fullUrl'))),
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, city, or country is missing',
                )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['url']
            ),
        )


def main():
    JaysonGillhamComCrawler().run()


if __name__ == '__main__':
    main()
