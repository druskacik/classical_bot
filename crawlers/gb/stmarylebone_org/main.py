import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://stmarylebone.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/product'
SOURCE = 'St Marylebone Parish Church'
VENUE = 'St Marylebone Parish Church'
CITY = 'London'
MUSIC_CATEGORY_ID = 25
WORSHIP_CATEGORY_ID = 24

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_LINE_RE = re.compile(
    r'^\s*(\d{1,2}\s+[A-Za-z]+\s+20\d{2}),\s*'
    r'(\d{1,2})[.:](\d{2})\s*-\s*'
    r'(?:(\d{1,2}\s+[A-Za-z]+\s+20\d{2}),\s*)?'
    r'(\d{1,2})[.:](\d{2})\s*$',
    re.IGNORECASE,
)
OVERVIEW_EVENT_RE = re.compile(
    r'^(\d{1,2}\s+[A-Za-z]+\s+20\d{2}),\s*'
    r'(\d{1,2})(?:[.:](\d{2}))?\s*-\s*'
    r'\d{1,2}(?:[.:]\d{2})?\s*(AM|PM)\s+(.+)$',
    re.IGNORECASE,
)
DAY_MONTH_EVENT_RE = re.compile(
    r'^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s*[–—-]\s*(.+)$',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    # The site's JavaScript challenge sets this fixed, non-secret cookie.
    session.cookies.set('SentryVerifiedJS', 'true', domain='stmarylebone.org', path='/')
    return session


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def product_feed(session):
    products = {}
    page = 1
    while True:
        response = get_response(
            session,
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'product_cat': f'{MUSIC_CATEGORY_ID},{WORSHIP_CATEGORY_ID}',
                '_fields': 'id,link,title,product_cat',
            },
        )
        for product in response.json():
            products[product['id']] = product
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return list(products.values())


def parse_date_line(text):
    match = DATE_LINE_RE.match(text)
    if not match:
        return None
    start_date, start_hour, start_minute, end_date, _, _ = match.groups()
    # Date ranges on this site are season/festival overview products rather
    # than one concrete occurrence. Individual performances are scraped when
    # they have their own product page.
    if end_date:
        return None
    try:
        event_date = datetime.strptime(start_date, '%d %B %Y').date().isoformat()
    except ValueError:
        return None
    hour = int(start_hour)
    minute = int(start_minute)
    if hour > 23 or minute > 59:
        return None
    return event_date, f'{hour:02d}:{minute:02d}'


def page_description(product):
    blocks = product.select('.col-md-8 .text .col-12')
    parts = []
    for block in blocks:
        text = clean_text(block)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_overview_events(product, url, overview_title):
    records = []
    for paragraph in product.select('.col-md-8 .text p'):
        text = re.sub(r'\s+', ' ', clean_text(paragraph))
        match = OVERVIEW_EVENT_RE.match(text)
        if not match:
            continue
        date_text, hour_text, minute_text, meridiem, title = match.groups()
        try:
            event_date = datetime.strptime(date_text, '%d %B %Y').date().isoformat()
        except ValueError:
            continue
        hour = int(hour_text)
        minute = int(minute_text or 0)
        if hour < 1 or hour > 12 or minute > 59:
            continue
        if meridiem.lower() == 'pm' and hour != 12:
            hour += 12
        elif meridiem.lower() == 'am' and hour == 12:
            hour = 0

        description_parts = []
        sibling = paragraph.find_next_sibling()
        while sibling and sibling.name == 'p':
            sibling_text = re.sub(r'\s+', ' ', clean_text(sibling))
            if OVERVIEW_EVENT_RE.match(sibling_text):
                break
            if sibling_text and not re.match(
                r'^(?:Reserve your place|Free Admission|Join our mailing list)',
                sibling_text,
                re.IGNORECASE,
            ):
                description_parts.append(sibling_text)
            sibling = sibling.find_next_sibling()

        records.append(
            {
                'title': title.strip(),
                'date': event_date,
                'url': url,
                'time_from': f'{hour:02d}:{minute:02d}',
                'venue': VENUE,
                'city': CITY,
                'country_code': 'GB',
                'description': '\n\n'.join(description_parts) or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    if records:
        return records

    year_match = re.search(r'\b(20\d{2})\b', overview_title)
    overview_text = clean_text(product)
    time_match = re.search(
        r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', overview_text, re.IGNORECASE
    )
    if not year_match or not time_match:
        return []
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    if time_match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif time_match.group(3).lower() == 'am' and hour == 12:
        hour = 0

    for line in overview_text.splitlines():
        match = DAY_MONTH_EVENT_RE.match(line.strip())
        if not match:
            continue
        day, month, title = match.groups()
        try:
            event_date = datetime.strptime(
                f'{day} {month} {year_match.group(1)}', '%d %B %Y'
            ).date().isoformat()
        except ValueError:
            continue
        records.append(
            {
                'title': title.strip(),
                'date': event_date,
                'url': url,
                'time_from': f'{hour:02d}:{minute:02d}',
                'venue': VENUE,
                'city': CITY,
                'country_code': 'GB',
                'description': overview_text,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def parse_product(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    product = soup.select_one('main .product.type-product')
    header = soup.select_one('.image-text-header .section-title')
    if not product or not header:
        return []

    title = clean_text(header.select_one('h1, h2'))
    date_node = header.select_one('p.ft-38')
    date_line = clean_text(date_node)
    parsed = parse_date_line(date_line)
    if not title:
        return []
    if not parsed:
        # A small number of season products contain concrete, individually
        # dated performances in their body. Expand those occurrences rather
        # than emitting the overview's broad date range.
        if DATE_LINE_RE.match(date_line):
            return parse_overview_events(product, url, title)
        return []

    description = page_description(product)
    evidence = f'{title}\n{description or ""}'
    # Do not apply the parish church default to explicitly off-site events.
    if re.search(r'\b(?:St Albans|Westminster Abbey|off[ -]site|pilgrimage)\b', evidence, re.I):
        return []

    event_date, time_from = parsed
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }]


def get_concerts():
    session = make_session()
    products = product_feed(session)
    records = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(get_response, session, product['link']): product['link']
            for product in products
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_product(future.result().content, url))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape St Marylebone event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class StMaryleboneOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stmarylebone_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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
    StMaryleboneOrgCrawler().run()


if __name__ == '__main__':
    main()
