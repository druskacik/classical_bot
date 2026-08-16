import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://gulfshoreopera.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/product'
SOURCE = 'Gulfshore Opera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_CITIES = {
    'Artis Naples': 'Naples',
    'Artis–Naples': 'Naples',
    'Barbara B. Mann PAH': 'Fort Myers',
    'Charlotte Performing Arts Center': 'Punta Gorda',
    'Club at the Strand': 'Naples',
    'Colony Bay Club': 'Bonita Springs',
    'Daniels Pavilion, Artis–Naples': 'Naples',
    'Punta Gorda Woman\'s Club Building': 'Punta Gorda',
    'Punta Gorda Woman’s Club Building': 'Punta Gorda',
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    raw = str(value)
    text = (
        BeautifulSoup(raw, 'html.parser').get_text(separator, strip=True)
        if '<' in raw
        else unescape(raw)
    )
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    normalized = re.sub(r'(\d)(?:st|nd|rd|th)', r'\1', value, flags=re.I)
    normalized = normalized.replace(',', '')
    match = re.search(r'[A-Z][a-z]+\s+\d{1,2}\s+20\d{2}', normalized)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    range_match = re.search(
        r'\b(\d{1,2})(?::(\d{2}))?\s+to\s+\d{1,2}(?::\d{2})?\s*([AP]M)\b',
        value,
        re.I,
    )
    if range_match:
        hour = int(range_match.group(1))
        # A range such as "11:00 to 2:30 PM" starts before noon.
        if range_match.group(3).upper() == 'PM' and hour < 8:
            hour += 12
        return f'{hour:02d}:{range_match.group(2) or "00"}'
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP]M)\b', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'PM':
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def parse_location(value):
    location = value.split('|', 1)[0].strip(' ,-')
    location = re.sub(r'\s+[\u2013\u2014-]\s+with\b.*$', '', location, flags=re.I)
    if location in VENUE_CITIES:
        venue, city = location, VENUE_CITIES[location]
    elif ',' in location:
        venue, city = [part.strip() for part in location.rsplit(',', 1)]
    else:
        venue, city = location, VENUE_CITIES.get(location, '')
    return venue, city


def parse_product(product):
    title_html = product.get('title', {}).get('rendered', '')
    parts = [clean_text(part) for part in re.split(r'<br\s*/?>|</br\s*>', title_html, flags=re.I)]
    parts = [part for part in parts if part]
    if len(parts) < 3:
        return None

    event_date = parse_date(parts[0])
    title = parts[1]
    location_text = ' '.join(parts[2:])
    venue, city = parse_location(location_text)
    url = clean_text(product.get('link'))
    description = clean_text(product.get('content', {}).get('rendered', ''), '\n')
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(location_text),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_products():
    session = requests.Session()
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
                'order': 'asc',
                '_fields': 'id,link,title,content',
            },
            timeout=45,
        )
        response.raise_for_status()
        products = response.json()
        for product in products:
            record = parse_product(product)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Gulfshore Opera product',
                    event='crawler_item_skipped',
                    level='warning',
                    url=clean_text(product.get('link')),
                    error_type='IncompleteEventData',
                    error_message='Required date, title, URL, venue, or city is missing',
                )
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return records


class GulfshoreOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gulfshoreopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return sorted(
            get_products(),
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    GulfshoreOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
