import html
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://gainesvilleorchestra.com/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-tickets/')
SOURCE = 'The Gainesville Orchestra'

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
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def find_event_schema(value):
    if isinstance(value, dict):
        schema = value.get('eventSchema')
        if isinstance(schema, str):
            try:
                parsed = json.loads(schema)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get('@type') == 'Event':
                return parsed
        for child in value.values():
            result = find_event_schema(child)
            if result:
                return result
    elif isinstance(value, list):
        for child in value:
            result = find_event_schema(child)
            if result:
                return result
    return None


def parse_event_schema(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get('@type') == 'Event':
            return value

    next_data = soup.select_one('#__NEXT_DATA__')
    if next_data:
        try:
            return find_event_schema(json.loads(next_data.string or next_data.get_text()))
        except json.JSONDecodeError:
            pass
    return None


def listing_entries(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    entries = []
    seen = set()
    for link in soup.select('a[href*="showpass.com/the-gainesville-orchestra-"]'):
        url = link.get('href', '').split('#', 1)[0]
        if not url or url in seen:
            continue
        seen.add(url)
        image = link.find('img')
        entries.append((url, clean_text(image.get('alt')) if image else ''))
    return entries


def additional_dates(image_alt, first_date):
    """Read an explicitly advertised same-month range such as May 7-8, 2027."""
    match = re.search(
        r'\b([A-Z][a-z]+)\s+(\d{1,2})\s*[–-]\s*(\d{1,2}),?\s+(\d{4})\b',
        image_alt,
    )
    if not match:
        return []
    month, start_day, end_day, year = match.groups()
    try:
        start = datetime.strptime(f'{month} {start_day} {year}', '%B %d %Y').date()
        end = datetime.strptime(f'{month} {end_day} {year}', '%B %d %Y').date()
    except ValueError:
        return []
    if start.isoformat() != first_date or end <= start or (end - start).days > 7:
        return []
    return [(start + timedelta(days=offset)).isoformat() for offset in range(1, (end - start).days + 1)]


def record_from_schema(schema, url):
    start_value = schema.get('startDate')
    try:
        start = datetime.fromisoformat(start_value)
    except (TypeError, ValueError):
        return None

    location = schema.get('location') or {}
    address = location.get('address') or {}
    title = clean_text(schema.get('name'))
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))
    if not title or not venue or not city:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': clean_text(schema.get('description')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    entries = listing_entries(response.text)

    records = []
    for url, image_alt in entries:
        try:
            detail = session.get(url, timeout=45)
            detail.raise_for_status()
            schema = parse_event_schema(detail.text)
            record = record_from_schema(schema or {}, url)
            if not record:
                log_message(
                    'Concert detail lacked required structured fields',
                    event='crawler_record_skipped',
                    level='warning',
                    url=url,
                )
                continue
            records.append(record)
            for event_date in additional_dates(image_alt, record['date']):
                event_day = str(int(event_date[-2:]))
                range_title = re.sub(r'\b\d{1,2}$', event_day, record['title'])
                records.append({**record, 'date': event_date, 'title': range_title})
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class GainesvilleOrchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gainesvilleorchestra_com',
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
        return scrape_concerts()


def main():
    GainesvilleOrchestraComCrawler().run()


if __name__ == '__main__':
    main()
