import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.pcmsconcerts.org/'
SOURCE = 'Philadelphia Chamber Music Society'
CITY = 'Philadelphia'
COUNTRY_CODE = 'US'

# Public, search-only credentials published by the site's Algolia integration.
ALGOLIA_APP_ID = 'W1RXWVXKIB'
ALGOLIA_SEARCH_KEY = '19a56a603b66c79850a2a3ac6faf5ce6'
ALGOLIA_INDEX = 'pcms_new_redesignposts_product'
ALGOLIA_URL = (
    f'https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/'
    f'{ALGOLIA_INDEX}/query'
)

HEADERS = {
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'X-Algolia-Application-Id': ALGOLIA_APP_ID,
    'X-Algolia-API-Key': ALGOLIA_SEARCH_KEY,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(html.unescape(str(value)), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(str(value).strip(), '%m/%d/%Y').date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    if not value:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def is_concert_candidate(hit):
    product_types = (hit.get('taxonomies') or {}).get('wc_product_type') or []
    # Current Perelman Theater performances use the operational "Seat Map"
    # product type, while other venues and the archive use "Concert".
    return bool({'Concert', 'Seat Map'}.intersection(product_types))


def make_record(hit):
    if not is_concert_candidate(hit):
        return None

    title = clean_text(hit.get('post_title'))
    event_date = parse_date(hit.get('event_date'))
    url = str(hit.get('permalink') or '').strip()
    venue = clean_text(hit.get('event_venue'))
    if not title or not event_date or not venue or not url.startswith(SOURCE_URL):
        return None

    description_parts = []
    for value in (hit.get('post_excerpt'), hit.get('content')):
        text = clean_text(value)
        if text and text not in description_parts:
            description_parts.append(text)

    city = 'Haverford' if 'Haverford College' in venue else CITY
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(hit.get('event_time')),
        'venue': venue,
        'city': city,
        'country_code': COUNTRY_CODE,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_index_hits(session):
    def get_range(lower, upper):
        numeric_filters = [f'post_date >= {lower}', f'post_date < {upper}']
        response = session.post(
            ALGOLIA_URL,
            json={
                'query': '',
                'hitsPerPage': 200,
                'page': 0,
                'numericFilters': numeric_filters,
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        first_page = payload.get('hits') or []
        if not isinstance(first_page, list):
            raise ValueError('Algolia response did not contain a hits list')

        # Algolia search-only keys cap pagination at 1,000 results. Partitioning
        # by the indexed publication timestamp retains the complete archive.
        total = int(payload.get('nbHits') or 0)
        if total > 1000:
            midpoint = (lower + upper) // 2
            if midpoint in {lower, upper}:
                raise ValueError('Unable to partition oversized Algolia result set')
            return get_range(lower, midpoint) + get_range(midpoint, upper)

        hits = list(first_page)
        page_count = int(payload.get('nbPages') or 0)
        for page in range(1, page_count):
            response = session.post(
                ALGOLIA_URL,
                json={
                    'query': '',
                    'hitsPerPage': 200,
                    'page': page,
                    'numericFilters': numeric_filters,
                },
                timeout=45,
            )
            response.raise_for_status()
            page_hits = response.json().get('hits') or []
            if not isinstance(page_hits, list):
                raise ValueError('Algolia response did not contain a hits list')
            hits.extend(page_hits)
        return hits

    return get_range(0, 4102444800)  # 1970-01-01 through 2100-01-01 UTC


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    hits = get_index_hits(session)
    records = [record for hit in hits if (record := make_record(hit))]

    if not records:
        log_message(
            'No concrete concerts found in search index',
            event='crawler_empty_listing',
            level='warning',
            url=ALGOLIA_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PcmsConcertsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pcmsconcerts_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
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
        return scrape_concerts()


def main():
    PcmsConcertsOrgCrawler().run()


if __name__ == '__main__':
    main()
