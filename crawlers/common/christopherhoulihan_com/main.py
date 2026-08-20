import html
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.christopherhoulihan.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Christopher Houlihan'
SITE_TIMEZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

COUNTRY_CODES = {
    'United States': 'US',
    'United Kingdom': 'GB',
}

US_STATE_PATTERN = re.compile(
    r'(?:,|\s)('
    r'AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|'
    r'MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|'
    r'WA|WV|WI|WY|DC'
    r')(?:,|\s|$)',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_datetime(milliseconds):
    try:
        value = float(milliseconds) / 1000
        parsed = datetime.fromtimestamp(value, tz=SITE_TIMEZONE)
    except (TypeError, ValueError, OSError, OverflowError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def country_code(location):
    country = clean_text(location.get('addressCountry'))
    if country in COUNTRY_CODES:
        return COUNTRY_CODES[country]
    address = ' '.join(
        clean_text(location.get(field))
        for field in ('addressLine1', 'addressLine2')
    )
    if US_STATE_PATTERN.search(address):
        return 'US'
    return None


def city_from_location(location):
    address_line2 = clean_text(location.get('addressLine2'))
    if address_line2:
        city = address_line2.split(',', 1)[0].strip()
        if city:
            return city

    address_line1 = clean_text(location.get('addressLine1'))
    match = re.search(
        r',\s*([^,]+?),\s*(?:'
        r'AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|'
        r'MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|'
        r'WA|WV|WI|WY|DC'
        r')\b',
        address_line1,
        re.I,
    )
    return match.group(1).strip() if match else ''


def infer_location(title, description, venue, city, event_country):
    if not city:
        state_match = re.search(r'^(.+?),\s*[A-Z]{2}$', title)
        if state_match:
            city = state_match.group(1).strip()
            event_country = event_country or 'US'

    if title.lower().endswith(', new york'):
        city = city or 'New York'
        venue = venue or title.rsplit(',', 1)[0].strip()
        event_country = event_country or 'US'

    if city == 'Hartford' and 'Trinity College Organ Series' in description:
        venue = venue or 'Trinity College Chapel'
        event_country = event_country or 'US'

    return venue, city, event_country


def parse_event(item):
    title = clean_text(item.get('title'))
    path = clean_text(item.get('fullUrl'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = city_from_location(location)
    event_country = country_code(location)
    event_date, time_from = event_datetime(item.get('startDate'))
    description = clean_text(item.get('body'))
    venue, city, event_country = infer_location(
        title, description, venue, city, event_country
    )

    if not all((title, path, event_date, venue, city, event_country)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, path),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': event_country,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ChristopherHoulihanComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='christopherhoulihan_com',
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
        records = []
        offset = None
        seen_offsets = set()

        while True:
            params = {'format': 'json'}
            if offset is not None:
                params['offset'] = offset
            response = requests.get(
                CALENDAR_URL,
                params=params,
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()

            for item in payload.get('upcoming', []) + payload.get('past', []):
                record = parse_event(item)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Christopher Houlihan calendar item',
                        event='crawler_item_skipped',
                        level='warning',
                        url=urljoin(SOURCE_URL, clean_text(item.get('fullUrl'))),
                        error_type='IncompleteEventData',
                        error_message='Required date, title, URL, venue, city, or country is missing',
                    )

            pagination = payload.get('pagination') or {}
            if not pagination.get('nextPage'):
                break
            offset = pagination.get('nextPageOffset')
            if offset is None or offset in seen_offsets:
                log_message(
                    'Stopped Christopher Houlihan pagination at invalid repeated offset',
                    event='crawler_pagination_stopped',
                    level='warning',
                    url=response.url,
                    error_type='InvalidPaginationOffset',
                    error_message='The next page offset was absent or had already been visited',
                )
                break
            seen_offsets.add(offset)

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    ChristopherHoulihanComCrawler().run()


if __name__ == '__main__':
    main()
