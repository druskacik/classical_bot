import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chicagophilharmonic.org/'
AJAX_URL = f'{SOURCE_URL}wp-admin/admin-ajax.php'
SOURCE = 'Chicago Philharmonic'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(value):
    if not value:
        return ''
    value = unescape(str(value))
    text = (
        BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
        if '<' in value and '>' in value else value
    )
    text = text.replace('\xa0', ' ').replace('\u3164', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def request_data(feed, comparison, timestamp, page):
    order = 'asc' if comparison == '>=' else 'desc'
    shortcode = [
        'masonry', 'b', feed, 'link', 'a', 'all', '', '', '', '', '',
        order, '100', 'lightbox', '4', '4', 'L1', 'main',
    ]
    data = [('action', 'eosa_ajax_getEvents')]
    data.extend(('arrSc[]', value) for value in shortcode)
    data.extend([
        ('arrSc[18][0][key]', 'evcal_erow'),
        ('arrSc[18][0][value]', str(timestamp)),
        ('arrSc[18][0][compare]', comparison),
        ('arrSc[]', '3|no'),
        ('arrSc[]', '15'),
        ('paged', str(page)),
    ])
    return data


def listing_events(session):
    # Use one boundary for both feeds so an event cannot move between them
    # during the scrape. A larger page size keeps the extensive archive cheap.
    boundary = int(datetime.now().timestamp())
    events = []
    seen_ids = set()
    for feed, comparison in (('all_events', '>='), ('past_concerts', '<')):
        for page in range(1, 1000):
            response = session.post(
                AJAX_URL,
                data=request_data(feed, comparison, boundary, page),
                timeout=60,
            )
            response.raise_for_status()
            page_events = response.json()
            if not page_events:
                break
            for event in page_events:
                event_id = event[17] if len(event) > 17 else None
                if event_id not in seen_ids:
                    seen_ids.add(event_id)
                    events.append(event)
            if len(page_events) < 100:
                break
        else:
            raise RuntimeError(f'EventON pagination did not terminate for {feed}')
    return events


def canonical_url(event):
    if len(event) <= 11:
        return ''
    return clean_text(event[11]).split('?', 1)[0]


def event_schema(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('@type') == 'Event':
                return candidate
            if isinstance(candidate, dict):
                for item in candidate.get('@graph') or []:
                    if isinstance(item, dict) and item.get('@type') == 'Event':
                        return item
    return {}


def city_from_address(address, description):
    address = clean_text(address)
    description = clean_text(description)
    patterns = (
        r'\b(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Boulevard|Blvd|Lane|Ln)\.?\s+'
        r'([A-Za-z][A-Za-z .\'’-]+),?\s+[A-Z]{2}\s+\d{5}\b',
        r',\s*([A-Za-z][A-Za-z .\'’-]+),\s*[A-Z]{2}\s+\d{5}\b',
    )
    for text in (address, description):
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return clean_text(match.group(1))
    # The calendar is overwhelmingly local, and bare Chicago street addresses
    # are used on several records. Touring records explicitly name another city.
    if re.search(r'\b(?:IL|Illinois)\b', address) or re.search(
        r'\bChicago,?\s+(?:IL|Illinois)\b', description, re.I
    ):
        return 'Chicago'
    return ''


def make_record(event, schema):
    title = clean_text(schema.get('name') or (event[2] if len(event) > 2 else ''))
    url = canonical_url(event) or clean_text(schema.get('url'))
    start = clean_text(schema.get('startDate'))
    timestamp = event[15] if len(event) > 15 else None
    try:
        start_at = datetime.fromtimestamp(int(timestamp))
    except (TypeError, ValueError, OSError):
        match = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})(?:T(\d{1,2}):(\d{2}))?', start)
        if not match:
            return None
        year, month, day, hour, minute = match.groups()
        start_at = datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0)
        )

    locations = schema.get('location') or []
    if isinstance(locations, dict):
        locations = [locations]
    location = locations[0] if locations and isinstance(locations[0], dict) else {}
    venue = clean_text(location.get('name'))
    address_data = location.get('address') or {}
    if isinstance(address_data, dict):
        address = clean_text(address_data.get('streetAddress'))
        city = clean_text(address_data.get('addressLocality'))
    else:
        address = clean_text(address_data)
        city = ''
    description = clean_text(schema.get('description')) or (
        clean_text(event[7]) if len(event) > 7 else ''
    )
    city = city or city_from_address(address, description)

    if not venue and len(event) > 5:
        venue = clean_text(str(event[5]).split('|', 1)[0].split(',', 1)[0])
    if not city and len(event) > 5:
        city = city_from_address(str(event[5]).split('|', 1)[0], description)
    if not title or not url or not venue or not city:
        return None
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'description': description or None,
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    events = listing_events(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(session.get, canonical_url(event), timeout=60): event
            for event in events if canonical_url(event)
        }
        for future in as_completed(futures):
            event = futures[future]
            url = canonical_url(event)
            try:
                response = future.result()
                response.raise_for_status()
                schema = event_schema(response.text)
                record = make_record(event, schema)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Chicago Philharmonic event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                record = make_record(event, {})
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ChicagoPhilharmonicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chicagophilharmonic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ChicagoPhilharmonicOrgCrawler().run()


if __name__ == '__main__':
    main()
