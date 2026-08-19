import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.pasinfonia.org/'
SOURCE = 'Pennsylvania Sinfonia Orchestra'
COUNTRY_CODE = 'US'
PERFORMANCE_URLS = [
    f'{SOURCE_URL}pennsylvania-sinfonia/',
    f'{SOURCE_URL}valley-vivaldi/',
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    value = str(value)
    text = (
        BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
        if re.search(r'<[^>]+>', value)
        else value
    )
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_start(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r'(\d{4})-(\d{1,2})-(\d{1,2})T(\d{1,2}):(\d{2})(?::\d{2})?(?:Z|[+-]\d{1,2}:\d{2})?',
        value.strip(),
    )
    if not match:
        return None
    try:
        parsed = datetime(*map(int, match.groups()))
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def first_location(value):
    if isinstance(value, list):
        return value[0] if value else {}
    return value if isinstance(value, dict) else {}


def city_from_location(location):
    address = location.get('address') or {}
    if isinstance(address, dict):
        city = clean_text(address.get('addressLocality'))
        address_text = clean_text(address.get('streetAddress'))
    else:
        city = ''
        address_text = clean_text(address)
    if city:
        return city

    # EventOn stores these US addresses as "street, City, PA [ZIP]".
    match = re.search(r',\s*([^,]+),\s*PA(?:\s+\d{5}(?:-\d{4})?)?\s*$', address_text, re.I)
    return clean_text(match.group(1)) if match else ''


def start_from_event_node(event_node, schema_start):
    displayed = event_node.select_one('.evo_eventcard_time_t') if event_node else None
    displayed = clean_text(displayed.get_text(' ', strip=True)) if displayed else ''
    match = re.match(r'([A-Za-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2} [ap]m)', displayed, re.I)
    if match:
        try:
            parsed = datetime.strptime(match.group(1), '%B %d, %Y %I:%M %p')
            return parsed.date().isoformat(), parsed.strftime('%H:%M')
        except ValueError:
            pass
    return parse_start(schema_start)


def record_from_schema(data, event_node):
    if not isinstance(data, dict) or data.get('@type') != 'Event':
        return None

    title = clean_text(data.get('name'))
    url = clean_text(data.get('url'))
    start = start_from_event_node(event_node, data.get('startDate'))
    location = first_location(data.get('location'))
    venue = clean_text(location.get('name'))
    city = city_from_location(location)
    if not title or not url or not start or not venue or not city:
        return None

    event_date, time_from = start
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': clean_text(data.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records_by_occurrence = {}

    for listing_url in PERFORMANCE_URLS:
        response = session.get(listing_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        schemas = soup.select(
            '.eventon_list_event[data-event_id] script[type="application/ld+json"]'
        )
        for schema in schemas:
            try:
                data = json.loads(schema.string or schema.get_text())
            except (TypeError, json.JSONDecodeError) as error:
                log_message(
                    'Skipping invalid event schema',
                    event='crawler_invalid_event_schema',
                    level='warning',
                    url=listing_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = record_from_schema(data, schema.find_parent(attrs={'data-event_id': True}))
            if record:
                key = (record['url'], record['date'], record['time_from'], record['venue'])
                records_by_occurrence[key] = record

    records = sorted(
        records_by_occurrence.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )
    if not records:
        log_message(
            'No concerts found on performance pages',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return records


class PaSinfoniaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pasinfonia_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PaSinfoniaOrgCrawler().run()


if __name__ == '__main__':
    main()
