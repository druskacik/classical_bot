import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chambermusicoc.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/product'
SOURCE = 'Chamber Music | OC'
HOME_CITY = 'Lake Forest'
HOME_VENUE = 'Chamber Music | OC'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

DATE_TIME_RE = re.compile(
    r'(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s*@\s*'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[ap]\.?(?:m\.?)?)',
    re.IGNORECASE,
)

# These phrases identify performances away from CMOC's Lake Forest facility.
# The site does not publish a structured venue for them, so they must not be
# assigned the otherwise defensible home-venue default.
TOURING_MARKERS = (
    'balboa island classical concert series',
    'classical crossroads',
    'community showcase series',
    'community engagement:',
    'hemet concert association',
    'heritage pointe',
    'waverly chapel',
)


def clean_html(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_occurrence(value):
    match = DATE_TIME_RE.search(clean_html(value))
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group('date'), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None, None

    normalized_time = re.sub(r'\.', '', match.group('time')).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return event_date, datetime.strptime(normalized_time, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return event_date, None


def is_unlocated_touring_event(title, description):
    evidence = f'{title}\n{description}'.lower()
    return any(marker in evidence for marker in TOURING_MARKERS)


def product_to_record(product):
    title = clean_html(product.get('title', {}).get('rendered'))
    excerpt = clean_html(product.get('excerpt', {}).get('rendered'))
    event_date, time_from = parse_occurrence(excerpt)
    url = product.get('link') or ''

    meta = product.get('meta') or {}
    body = clean_html(meta.get('_et_pb_old_content'))
    description_parts = [part for part in (excerpt, body) if part]
    description = '\n\n'.join(dict.fromkeys(description_parts)) or None

    if not title or not event_date or not url.startswith(('http://', 'https://')):
        return None
    if is_unlocated_touring_event(title, description or ''):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': HOME_VENUE,
        'city': HOME_CITY,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_products(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'desc',
                '_fields': 'id,link,title,excerpt,meta,product_cat',
            },
            timeout=45,
        )
        response.raise_for_status()
        products = response.json()
        for product in products:
            record = product_to_record(product)
            if record:
                records.append(record)

        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1

    if not records:
        log_message(
            'No parseable event products found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class ChamberMusicOcOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusicoc_org',
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
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_products()


def main():
    ChamberMusicOcOrgCrawler().run()


if __name__ == '__main__':
    main()
