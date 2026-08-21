import json
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.elliottcarter.com/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'events/archive/?range=all')
SOURCE = 'Elliott Carter'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def extract_archive_data(html):
    marker = 'var jsonData = '
    position = html.find(marker)
    if position < 0:
        raise ValueError('Embedded event data was not found')
    data, _ = json.JSONDecoder().raw_decode(html[position + len(marker):].lstrip())
    if not isinstance(data, list):
        raise ValueError('Embedded event data is not a list')
    return data


def parse_dates(value):
    soup = BeautifulSoup(value or '', 'html.parser')
    dates = []
    for item in soup.select('li'):
        raw = clean_text(item.get_text(' ', strip=True))
        try:
            parsed = datetime.strptime(raw, '%b %d, %Y %I:%M %p')
        except ValueError:
            continue
        # Midnight is the archive's placeholder for legacy records whose time
        # is unknown; the visible listing suppresses it.
        time_from = None if parsed.hour == 0 and parsed.minute == 0 else parsed.strftime('%H:%M')
        dates.append((parsed.date().isoformat(), time_from))
    return dates


def event_id(soup):
    dates = soup.select_one('.event-dates')
    if not dates:
        return ''
    match = re.search(r'\be\d+\b', dates.get_text(' ', strip=True))
    return match.group(0) if match else ''


def schema_data(soup):
    script = soup.select_one('script[type="application/ld+json"]')
    if not script or not script.string:
        return {}
    try:
        data = json.loads(script.string)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_location(item, soup, schema):
    country_code = item.get('country') or ''
    if not re.fullmatch(r'[A-Z]{2}', country_code):
        return None

    location = schema.get('location') or {}
    address = location.get('address') or {}
    venue = clean_text(location.get('name'))
    city = clean_text(address.get('addressLocality'))

    # Some older records predate the site's JSON-LD. A non-placeholder venue
    # identifier proves the first comma-delimited component is a venue name.
    if not venue or not city:
        location_node = soup.select_one('.event-location')
        venue_id = item.get('venue') or ''
        if location_node and venue_id not in ('', 'v00'):
            location_copy = BeautifulSoup(str(location_node), 'html.parser')
            separator = location_copy.select_one('.entry-separator')
            if separator:
                separator.extract()
            more_info = location_copy.find('a', string=re.compile(r'More Info', re.I))
            if more_info:
                more_info.extract()
            parts = [clean_text(part) for part in location_copy.get_text(' ', strip=True).split(',')]
            parts = [part for part in parts if part]
            if len(parts) >= 2:
                venue = venue or parts[0]
                city = city or parts[1]

    if not venue or not city or venue.casefold() == city.casefold():
        return None
    return venue, city, country_code


def make_records(item):
    display = (item.get('info') or {}).get('display') or ''
    soup = BeautifulSoup(display, 'html.parser')
    schema = schema_data(soup)
    title = clean_text(schema.get('name')) or clean_text(
        soup.select_one('.event-title').get_text(' ', strip=True)
        if soup.select_one('.event-title') else ''
    )
    location = resolve_location(item, soup, schema)
    dates = parse_dates(item.get('dates'))
    identifier = event_id(soup)
    if not title or not location or not dates or not identifier:
        return []

    permalink = soup.select_one('.mssb-permalink a[href]')
    url = urljoin(SOURCE_URL, permalink['href']) if permalink else f'{ARCHIVE_URL}&event={identifier}'

    description_parts = []
    body = clean_text(schema.get('description')) or clean_text(
        soup.select_one('.moretext').get_text('\n', strip=True)
        if soup.select_one('.moretext') else ''
    )
    works = clean_text(
        soup.select_one('.event-works').get_text('\n', strip=True)
        if soup.select_one('.event-works') else ''
    )
    if body:
        description_parts.append(body)
    if works:
        description_parts.append(f'Works\n{works}')
    description = '\n\n'.join(description_parts) or None
    venue, city, country_code = location

    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
        }
        for event_date, time_from in dates
    ]


def get_concerts():
    response = requests.get(ARCHIVE_URL, headers=HEADERS, timeout=90)
    response.raise_for_status()
    items = extract_archive_data(response.text)
    records = []
    accepted_items = 0
    for item in items:
        item_records = make_records(item)
        if item_records:
            accepted_items += 1
            records.extend(item_records)

    skipped_count = len(items) - accepted_items
    if skipped_count:
        log_message(
            'Skipped archive entries without a valid date, venue, city, or country',
            event='crawler_items_skipped',
            level='info',
            record_count=skipped_count,
        )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class ElliottcarterComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='elliottcarter_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ElliottcarterComCrawler().run()


if __name__ == '__main__':
    main()
