import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://philarmonia.com.ua/'
PRODUCTS_API = f'{SOURCE_URL}wp-json/wp/v2/product'
SOURCE = 'National Philharmonic of Ukraine'
DEFAULT_CITY = 'Kyiv'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'uk-UA,uk;q=0.9,en;q=0.7',
}

MONTHS = {
    'січня': 1,
    'лютого': 2,
    'березня': 3,
    'квітня': 4,
    'травня': 5,
    'червня': 6,
    'липня': 7,
    'серпня': 8,
    'вересня': 9,
    'жовтня': 10,
    'листопада': 11,
    'грудня': 12,
}

WEEKDAYS = {
    'понеділок': 0,
    'вівторок': 1,
    'середа': 2,
    'четвер': 3,
    "п'ятниця": 4,
    'п’ятниця': 4,
    'субота': 5,
    'неділя': 6,
}

CITY_PATTERNS = {
    'Kyiv': ('київ', 'kyiv', 'kiev'),
    'Lviv': ('львів', 'lviv'),
    'Odesa': ('одеса', 'odesa', 'odessa'),
    'Kharkiv': ('харків', 'kharkiv'),
    'Dnipro': ('дніпро', 'dnipro'),
    'Zaporizhzhia': ('запоріжжя', 'zaporizhzhia'),
    'Vinnytsia': ('вінниця', 'vinnytsia'),
    'Chernihiv': ('чернігів', 'chernihiv'),
    'Poltava': ('полтава', 'poltava'),
    'Cherkasy': ('черкаси', 'cherkasy'),
    'Uzhhorod': ('ужгород', 'uzhhorod'),
    'Ivano-Frankivsk': ('івано-франківськ', 'ivano-frankivsk'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_products(session):
    products = []
    page = 1
    while True:
        response = session.get(
            PRODUCTS_API,
            params={
                'per_page': 100,
                'page': page,
                '_fields': 'id,date,link,title,product_cat,class_list',
            },
            timeout=45,
        )
        if response.status_code == 400 and page > 1:
            break
        response.raise_for_status()
        batch = response.json()
        products.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', page))
        if page >= total_pages:
            break
        page += 1
    return products


def resolve_date(date_text, published_at):
    normalized = clean_text(date_text).lower()
    match = re.search(r'(\d{1,2})\s+([а-яіїєґ]+)', normalized)
    if not match or match.group(2) not in MONTHS:
        return None

    day = int(match.group(1))
    month = MONTHS[match.group(2)]
    try:
        published = datetime.fromisoformat(published_at).date()
    except (TypeError, ValueError):
        return None

    weekday = next((number for name, number in WEEKDAYS.items() if name in normalized), None)
    candidates = []
    for year in range(published.year - 1, published.year + 3):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if weekday is not None and candidate.weekday() != weekday:
            continue
        candidates.append(candidate)
    if not candidates:
        return None

    # Events are normally published before their occurrence. Allow a short
    # delay for archived items that may have been entered just after the date.
    candidates.sort(key=lambda candidate: (candidate < published, abs((candidate - published).days)))
    chosen = candidates[0]
    if chosen < published and (published - chosen).days > 31:
        return None
    return chosen.isoformat()


def location_parts(soup, product):
    location = soup.select_one('.pr_info_txt')
    hall = soup.select_one('.gold_txt.mb40')
    venue = clean_text(hall) or clean_text(location)
    if not venue:
        return None, None

    location_block = clean_text(location.parent if location else '')
    lowered = location_block.lower()
    city = next(
        (city for city, patterns in CITY_PATTERNS.items() if any(pattern in lowered for pattern in patterns)),
        None,
    )

    classes = ' '.join(product.get('class_list') or []).lower()
    touring = 'product_cat-gastroli' in classes or 'гастрол' in lowered
    if not city and not touring:
        city = DEFAULT_CITY
    return venue, city


def detail_description(soup):
    parts = []
    for selector in ('.prod_content_section', '.pr_program'):
        value = clean_text(soup.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def make_record(product, response_text):
    soup = BeautifulSoup(response_text, 'html.parser')
    title = clean_text(soup.select_one('h1')) or clean_text((product.get('title') or {}).get('rendered'))
    date_node = soup.select_one('.product_date')
    event_date = resolve_date(clean_text(date_node), product.get('date'))
    venue, city = location_parts(soup, product)
    url = product.get('link') or ''
    if not title or not event_date or not url or not venue or not city:
        return None

    info_row = soup.select_one('.product_info_row')
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(info_row))
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'UA',
        'description': detail_description(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    products = get_products(session)
    records = []

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(session.get, product.get('link'), timeout=45): product
            for product in products if product.get('link')
        }
        for future in as_completed(futures):
            product = futures[future]
            url = product.get('link')
            try:
                response = future.result()
                response.raise_for_status()
                record = make_record(product, response.text)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class PhilarmoniaComUaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='philarmonia_com_ua',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='UA',
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
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    PhilarmoniaComUaCrawler().run()


if __name__ == '__main__':
    main()
