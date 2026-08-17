import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pensacolasymphony.com/'
SOURCE = 'Pensacola Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/product'
PERFORMANCE_CATEGORY_ID = 27
AUXILIARY_PRODUCT_CATEGORY_IDS = {92, 93, 95}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# PSO performs throughout Escambia and Santa Rosa counties.  Pensacola is a
# defensible default only when a page does not identify one of those tour stops.
PLACE_CITIES = {
    'century': 'Century',
    'gulf breeze': 'Gulf Breeze',
    'milton': 'Milton',
    'molino': 'Molino',
    'navarre': 'Navarre',
    'pace': 'Pace',
    'pensacola beach': 'Pensacola Beach',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    r'([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def extract_venue(soup):
    for label in soup.select('.pso_meta_label'):
        if clean_text(label).lower() != 'venue':
            continue
        container = label.parent
        value = clean_text(container)
        value = re.sub(r'^venue\s*', '', value, flags=re.IGNORECASE).strip(' :-\n')
        value = value.split('\n', 1)[0]
        value = re.split(r',\s*\d{2,}\b', value, maxsplit=1)[0].strip(' ,')
        if value:
            return value
    return None


def infer_city(venue, title, description):
    evidence = ' '.join((venue, title, description or '')).lower()
    for marker, city in PLACE_CITIES.items():
        if marker in evidence:
            return city
    return 'Pensacola'


def fetch_products(session):
    products = []
    page = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'product_cat': PERFORMANCE_CATEGORY_ID,
                'per_page': 100,
                'page': page,
                '_fields': 'id,link,slug,title,content,excerpt,product_cat',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        products.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            return products
        page += 1


def parse_product(session, product):
    if AUXILIARY_PRODUCT_CATEGORY_IDS.intersection(product.get('product_cat', [])):
        return None
    url = product.get('link', '')
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch performance detail',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    date_node = soup.select_one('.pso_performance_date')
    title_node = soup.select_one('h1.product_title')
    venue = extract_venue(soup)
    event_date = parse_date(date_node)
    title = clean_text(title_node) or clean_text(product.get('title', {}).get('rendered'))

    # Pages without the site's event date block are packages, sponsorships, or
    # other products rather than concrete occurrences.
    if not title or not event_date or not venue or not url.startswith(('http://', 'https://')):
        return None

    description_parts = [
        clean_text(product.get('content', {}).get('rendered')),
        clean_text(product.get('excerpt', {}).get('rendered')),
    ]
    description_parts = [part for part in description_parts if part]
    description = '\n\n'.join(dict.fromkeys(description_parts)) or None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(date_node),
        'venue': venue,
        'city': infer_city(venue, title, description),
        'country_code': 'US',
        'description': description,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    products = fetch_products(session)
    records = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(parse_product, session, product) for product in products]
        for future in as_completed(futures):
            record = future.result()
            if record:
                records.append(record)

    if not records:
        log_message(
            'No dated performance pages found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PensacolaSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pensacolasymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PensacolaSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
