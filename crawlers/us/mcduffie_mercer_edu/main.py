import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mcduffie.mercer.edu/'
CALENDAR_URL = urljoin(SOURCE_URL, 'concerts')
API_URL = urljoin(SOURCE_URL, 'api/open/GetItemsByMonth')
SOURCE = 'McDuffie Center for Strings'
TIMEZONE = ZoneInfo('America/New_York')
FIRST_CALENDAR_YEAR = 2020

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

KNOWN_VENUES = {
    'fickling hall': 'Fickling Hall',
    'grand opera house': 'The Grand Opera House',
    'bell house': 'The Bell House',
}


def clean_text(value):
    if not value:
        return ''
    soup = BeautifulSoup(html.unescape(str(value)), 'html.parser')
    text = soup.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_values():
    today = date.today()
    # The collection was created in 2020. Look three years ahead so events
    # published unusually early are not omitted.
    for year in range(FIRST_CALENDAR_YEAR, today.year + 4):
        for month in range(1, 13):
            yield f'{month:02d}-{year}'


def extract_collection_id(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    block = soup.select_one('.sqs-block-calendar[data-block-json]')
    if block:
        match = re.search(r'"collectionId"\s*:\s*"([a-zA-Z0-9]+)"', html.unescape(block['data-block-json']))
        if match:
            return match.group(1)
    raise ValueError('Could not find the Squarespace calendar collection ID')


def fetch_month(session, collection_id, month):
    response = session.get(
        API_URL,
        params={'month': month, 'collectionId': collection_id},
        headers={'X-Requested-With': 'XMLHttpRequest'},
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f'Unexpected calendar response for {month}')
    return payload


def calendar_items(session, collection_id):
    items = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(fetch_month, session, collection_id, month): month
            for month in month_values()
        }
        for future in as_completed(futures):
            month = futures[future]
            try:
                items.extend(future.result())
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch McDuffie calendar month',
                    event='crawler_item_failed',
                    level='warning',
                    url=API_URL,
                    month=month,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return items


def parse_city(address_lines):
    for line in address_lines:
        match = re.match(r'\s*([^,]+),\s*[A-Z]{2}(?:,|\s|$)', line)
        if match:
            return clean_text(match.group(1))
    return None


def infer_venue(description):
    lowered = description.lower()
    for needle, venue in KNOWN_VENUES.items():
        if needle in lowered:
            return venue
    return None


def parse_detail(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    content = soup.select_one('.eventitem-column-content')
    description = clean_text(content) or None
    venue_node = soup.select_one('.eventitem-meta-address-line--title')
    address_lines = [clean_text(node) for node in soup.select('.eventitem-meta-address-line')]
    venue = clean_text(venue_node) or None
    city = parse_city(address_lines)
    return description, venue, city


def fetch_detail(session, item):
    url = urljoin(SOURCE_URL, item.get('fullUrl') or '')
    if not item.get('fullUrl'):
        return item, None
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return item, parse_detail(response.text)
    except requests.RequestException as error:
        log_message(
            'Failed to fetch McDuffie concert detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return item, None


def make_record(item, detail=None):
    title = clean_text(item.get('title'))
    relative_url = item.get('fullUrl') or ''
    start_ms = item.get('startDate')
    end_ms = item.get('endDate')
    if not title or not relative_url or not isinstance(start_ms, (int, float)):
        return None

    start = datetime.fromtimestamp(start_ms / 1000, TIMEZONE)
    end = datetime.fromtimestamp(end_ms / 1000, TIMEZONE) if isinstance(end_ms, (int, float)) else None
    location = item.get('location') or {}
    description = clean_text(item.get('excerpt')) or None
    venue = clean_text(location.get('addressTitle')) or None
    address_lines = [clean_text(location.get('addressLine2'))]
    city = parse_city(address_lines)

    if detail:
        detail_description, detail_venue, detail_city = detail
        if detail_description and len(detail_description) > len(description or ''):
            description = detail_description
        venue = detail_venue or venue
        city = detail_city or city

    # Early entries used Squarespace's empty default map location. Only infer
    # one of the calendar's named Macon halls when the event text says so.
    venue = venue or infer_venue(description or '')
    if venue and not city:
        city = 'Macon'
    if not venue or not city:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': urljoin(SOURCE_URL, relative_url),
        'time_from': start.strftime('%H:%M'),
        'time_to': end.strftime('%H:%M') if end else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class McduffieMercerEduCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mcduffie_mercer_edu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'time_to', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CALENDAR_URL, timeout=45)
            response.raise_for_status()
            collection_id = extract_collection_id(response.text)
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to initialize McDuffie concert calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        items = calendar_items(session, collection_id)
        records = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_detail, session, item) for item in items]
            for future in as_completed(futures):
                item, detail = future.result()
                record = make_record(item, detail)
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    McduffieMercerEduCrawler().run()


if __name__ == '__main__':
    main()
