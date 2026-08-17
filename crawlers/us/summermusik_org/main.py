import json
import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://summermusik.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/mec-events'
SOURCE = 'Summermusik (Cincinnati Chamber Orchestra)'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = clean_text(value)
    for pattern in ('%b %d %Y', '%B %d %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*[AP]M)\b', value)
    if not match:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(match.group(1), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def city_from_address(address):
    address = clean_text(address)
    match = re.search(r',\s*([^,]+?),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b', address)
    return clean_text(match.group(1)) if match else None


def event_schema(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = data.get('@graph', []) if isinstance(data, dict) else []
        candidates = [data, *candidates] if isinstance(data, dict) else data
        for item in candidates:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    schema = event_schema(soup)

    title_node = soup.select_one('.mec-single-title')
    date_node = soup.select_one('.mec-single-event-date .mec-events-abbr')
    time_node = soup.select_one('.mec-single-event-time .mec-events-abbr')
    location = soup.select_one('.mec-single-event-location')
    venue_node = location.select_one('.mec-meta-label') if location else None
    address_node = location.select_one('.mec-events-address, .mec-address') if location else None
    description_node = soup.select_one('.mec-single-event-description')

    title = clean_text(title_node.get_text(' ', strip=True) if title_node else schema.get('name'))
    event_date = parse_date(date_node.get_text(' ', strip=True) if date_node else '')
    time_from = parse_time(time_node.get_text(' ', strip=True) if time_node else '')
    venue = clean_text(venue_node.get_text(' ', strip=True) if venue_node else '')
    address = clean_text(address_node.get_text(' ', strip=True) if address_node else '')

    schema_location = schema.get('location') if isinstance(schema.get('location'), dict) else {}
    if not event_date and schema.get('startDate'):
        try:
            event_date = datetime.fromisoformat(schema['startDate']).date().isoformat()
        except (TypeError, ValueError):
            pass
    if not venue:
        venue = clean_text(schema_location.get('name'))
    if not address:
        schema_address = schema_location.get('address')
        if isinstance(schema_address, dict):
            address = clean_text(schema_address.get('addressLocality'))
        else:
            address = clean_text(schema_address)
    city = city_from_address(address)
    if not city and isinstance(schema_location.get('address'), dict):
        city = clean_text(schema_location['address'].get('addressLocality')) or None

    description = clean_text(
        description_node.get_text('\n', strip=True) if description_node else ''
    ) or None

    if not all((title, event_date, venue, city, url)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event_urls(session):
    urls = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={'per_page': 100, 'page': page, '_fields': 'id,link'},
            timeout=45,
        )
        response.raise_for_status()
        items = response.json()
        urls.extend(item.get('link') for item in items if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


class SummermusikOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='summermusik_org',
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

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    def scrape(self):
        records = []
        urls = fetch_event_urls(self.session)
        for url in urls:
            try:
                response = self.session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_event_page(response.text, url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipping event with incomplete required fields',
                        event='crawler_event_skipped',
                        level='warning',
                        url=url,
                    )
            except requests.RequestException as error:
                log_message(
                    'Unable to fetch event detail',
                    event='crawler_event_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        log_message(
            'Summermusik scrape completed',
            event='crawler_scrape_completed',
            record_count=len(records),
            source_url=SOURCE_URL,
        )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    SummermusikOrgCrawler().run()


if __name__ == '__main__':
    main()
