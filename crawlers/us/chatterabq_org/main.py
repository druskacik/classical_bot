import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chatterabq.org/'
SOURCE = 'Chatter'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
SERIES_SLUGS = {'sunday', 'north', 'late-works', 'b-sides', 'cabaret-3'}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(r'\b([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b')
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\b', re.I)
AT_RE = re.compile(r'\s+[\u2013\u2014-]\s+.*?\b(?:at)\s+(.+)$', re.I)


def clean_html(value, separator=' '):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text(separator, strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(*values):
    for value in values:
        match = DATE_RE.search(clean_html(value))
        if not match:
            continue
        try:
            return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(*values):
    for value in values:
        match = TIME_RE.search(clean_html(value))
        if not match:
            continue
        normalized = match.group(1).replace('.', '').replace(' ', '').upper()
        for pattern in ('%I:%M%p', '%I%p'):
            try:
                return datetime.strptime(normalized, pattern).strftime('%H:%M')
            except ValueError:
                pass
    return None


def title_from_product(title, excerpt):
    excerpt_text = clean_html(excerpt)
    if excerpt_text:
        candidate = re.split(r'\s+[\u2013\u2014-]\s+(?=\d|\w+\s+at\b)', excerpt_text, maxsplit=1)[0]
        if candidate and not re.fullmatch(r'Chatter (?:Sunday|North|B-Sides)|Late Works', candidate, re.I):
            return candidate
    return clean_html(title)


def location_from_product(category_slugs, excerpt, content):
    text = clean_html(excerpt)
    venue_text = ''
    match = AT_RE.search(text)
    if match:
        venue_text = match.group(1).strip(' .')

    combined = f'{text} {clean_html(content)}'
    if 'Albuquerque Museum' in combined:
        return 'Albuquerque Museum', 'Albuquerque'
    if 'Unit B' in combined and 'Santa Fe' in combined:
        return 'Unit B', 'Santa Fe'
    if 'north' in category_slugs or 'CCA Santa Fe' in combined:
        return 'Center for Contemporary Arts', 'Santa Fe'
    if 'b-sides' in category_slugs and 'Santa Fe' in combined:
        return 'Unit B', 'Santa Fe'
    if '912 3rd St NW' in combined:
        return 'Chatter', 'Albuquerque'
    if venue_text and 'Santa Fe' in venue_text:
        return venue_text.replace(' in Santa Fe', '').strip(), 'Santa Fe'
    return None, None


def product_to_record(product, category_by_id):
    rendered_title = product.get('title', {}).get('rendered', '')
    rendered_excerpt = product.get('excerpt', {}).get('rendered', '')
    rendered_content = product.get('content', {}).get('rendered', '')
    category_slugs = {
        category_by_id.get(category_id, '') for category_id in product.get('product_cat', [])
    }

    event_date = parse_date(rendered_title, rendered_content, rendered_excerpt)
    venue, city = location_from_product(category_slugs, rendered_excerpt, rendered_content)
    title = title_from_product(rendered_title, rendered_excerpt)
    url = product.get('link', '')
    if not all((title, event_date, url, venue, city)):
        log_message(
            'Skipping product with incomplete event details',
            event='crawler_record_skipped',
            level='warning',
            url=url or SOURCE_URL,
            missing_date=not bool(event_date),
            missing_venue=not bool(venue),
            missing_city=not bool(city),
        )
        return None

    description_parts = [
        value for value in (clean_html(rendered_excerpt), clean_html(rendered_content)) if value
    ]
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(rendered_excerpt, rendered_content),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': '\n\n'.join(dict.fromkeys(description_parts)) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    category_response = session.get(
        f'{API_URL}/product_cat', params={'per_page': 100}, timeout=45
    )
    category_response.raise_for_status()
    categories = category_response.json()
    category_by_id = {item['id']: item['slug'] for item in categories}
    category_ids = sorted(
        item['id'] for item in categories if item.get('slug') in SERIES_SLUGS
    )
    missing_series = SERIES_SLUGS - set(category_by_id.values())
    if missing_series:
        log_message(
            'Expected concert series categories are missing',
            event='crawler_categories_missing',
            level='warning',
            url=f'{API_URL}/product_cat',
            missing_categories=sorted(missing_series),
        )
    if not category_ids:
        return []

    records = []
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/product',
            params={
                'per_page': 100,
                'page': page,
                'product_cat': ','.join(str(value) for value in category_ids),
                '_fields': 'id,link,title,content,excerpt,product_cat',
            },
            timeout=45,
        )
        response.raise_for_status()
        products = response.json()
        for product in products:
            record = product_to_record(product, category_by_id)
            if record:
                records.append(record)

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No concerts found in selected series',
            event='crawler_empty_listing',
            level='warning',
            url=f'{API_URL}/product',
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ChatterAbqOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chatterabq_org',
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
    ChatterAbqOrgCrawler().run()


if __name__ == '__main__':
    main()
