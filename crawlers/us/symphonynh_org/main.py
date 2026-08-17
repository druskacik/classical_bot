import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://symphonynh.org/'
EVENTS_URL = f'{SOURCE_URL}upcoming'
SOURCE = 'Symphony NH'
TIME_ZONE = ZoneInfo('America/New_York')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_city(location):
    address_line = clean_text((location or {}).get('addressLine2'))
    if not address_line:
        return ''
    return re.split(r',', address_line, maxsplit=1)[0].strip()


def parse_timestamp(value):
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=TIME_ZONE)


def parse_event(item):
    title = clean_text(item.get('title'))
    path = clean_text(item.get('fullUrl'))
    url = requests.compat.urljoin(SOURCE_URL, path)
    start = parse_timestamp(item.get('startDate'))
    location = item.get('location') or {}
    venue = clean_text(location.get('addressTitle'))
    city = parse_city(location)

    if not title or not path or not start or not venue or not city:
        return None

    description_parts = []
    for field in ('excerpt', 'body'):
        text = clean_text(item.get(field))
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


class SymphonyNhOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='symphonynh_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(
            EVENTS_URL,
            params={'format': 'json'},
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()

        records = []
        for item in payload.get('upcoming', []) + payload.get('past', []):
            record = parse_event(item)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Symphony NH event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=requests.compat.urljoin(
                        SOURCE_URL, clean_text(item.get('fullUrl'))
                    ),
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, or city is missing',
                )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    SymphonyNhOrgCrawler().run()


if __name__ == '__main__':
    main()
